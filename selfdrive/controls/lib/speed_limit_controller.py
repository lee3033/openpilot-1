from selfdrive.car.hyundai.interface import CarInterface
import time
from enum import IntEnum
import numpy as np
from cereal import car, log
from common.numpy_fast import interp
from common.params import Params
from common.realtime import sec_since_boot
from selfdrive.controls.lib.events import Events
from selfdrive.car.hyundai import interface

_PARAMS_UPDATE_PERIOD = 2.  # secs. Time between parameter updates.
_WAIT_TIME_LIMIT_RISE = 2.0  # Waiting time before raising the speed limit.

_SPEED_OFFSET_TH = -1.  # m/s Maximum offset between speed limit and current speed for adapting state.
_LIMIT_ADAPT_ACC = -1.2  # Ideal acceleration for the adapting (braking) phase when approaching speed limits.

_MAX_MAP_DATA_AGE = 10.0  # s Maximum time to hold to map data, then consider it invalid.

# Lookup table for speed limit percent offset depending on speed.
_LIMIT_PERC_OFFSET_V = [0.1, 0.05, 0.038]  # 55, 105, 135 km/h
_LIMIT_PERC_OFFSET_BP = [13.9, 27.8, 36.1]  # 50, 100, 130 km/h

SpeedLimitControlState = log.LongitudinalPlan.SpeedLimitControlState
EventName = car.CarEvent.EventName

_DEBUG = False


def _debug(msg):
  if not _DEBUG:
    return
  print(msg)


def _description_for_state(speed_limit_control_state):
  if speed_limit_control_state == SpeedLimitControlState.inactive:
    return 'INACTIVE'
  if speed_limit_control_state == SpeedLimitControlState.tempInactive:
    return 'TEMP_INACTIVE'
  if speed_limit_control_state == SpeedLimitControlState.adapting:
    return 'ADAPTING'
  if speed_limit_control_state == SpeedLimitControlState.active:
    return 'ACTIVE'


class SpeedLimitResolver():
  class Source(IntEnum):
    none = 0
    car_state = 1
    map_data = 2

  class Policy(IntEnum):
    car_state_only = 0
    map_data_only = 1
    car_state_priority = 2
    map_data_priority = 3
    combined = 4

  def __init__(self, policy=Policy.map_data_priority):
    self._limit_solutions = {}  # Store for speed limit solutions from different sources
    self._distance_solutions = {}  # Store for distance to current speed limit start for different sources
    self._v_ego = 0.
    self._current_speed_limit = 0.
    self._policy = policy
    self._next_speed_limit_prev = 0.
    self.speed_limit = 0.
    self.distance = 0.
    self.source = SpeedLimitResolver.Source.none

  def resolve(self, v_ego, current_speed_limit, sm):
    self._v_ego = v_ego
    self._current_speed_limit = current_speed_limit
    self._sm = sm

    self._get_from_car_state()
    self._get_from_map_data()
    self._consolidate()

    return self.speed_limit, self.distance, self.source

  def _get_from_car_state(self):
    self._limit_solutions[SpeedLimitResolver.Source.car_state] = self._sm['carState'].cruiseState.speedLimit
    self._distance_solutions[SpeedLimitResolver.Source.car_state] = 0.

  def _get_from_map_data(self):
    # Ignore if no live map data
    sock = 'liveMapData'
    if self._sm.logMonoTime[sock] is None:
      self._limit_solutions[SpeedLimitResolver.Source.map_data] = 0.
      self._distance_solutions[SpeedLimitResolver.Source.map_data] = 0.
      _debug('SL: No map data for speed limit')
      return

    # Load limits from map_data
    map_data = self._sm[sock]
    speed_limit = map_data.speedLimit if map_data.speedLimitValid else 0.
    next_speed_limit = map_data.speedLimitAhead if map_data.speedLimitAheadValid else 0.

    # Calculate the age of the gps fix. Ignore if too old.
    gps_fix_age = time.time() - map_data.lastGpsTimestamp * 1e-3
    if gps_fix_age > _MAX_MAP_DATA_AGE:
      self._limit_solutions[SpeedLimitResolver.Source.map_data] = 0.
      self._distance_solutions[SpeedLimitResolver.Source.map_data] = 0.
      _debug(f'SL: Ignoring map data as is too old. Age: {gps_fix_age}')
      return

    # When we have no ahead speed limit to consider or it is greater than current speed limit
    # or car has stopped, then provide current value and reset tracking.
    if next_speed_limit == 0. or self._v_ego <= 0. or next_speed_limit > self._current_speed_limit:
      self._limit_solutions[SpeedLimitResolver.Source.map_data] = speed_limit
      self._distance_solutions[SpeedLimitResolver.Source.map_data] = 0.
      self._next_speed_limit_prev = 0.
      return

    # Calculate the actual distance to the speed limit ahead corrected by gps_fix_age
    distance_since_fix = self._v_ego * gps_fix_age
    distance_to_speed_limit_ahead = max(0., map_data.speedLimitAheadDistance - distance_since_fix)

    # When we have a next_speed_limit value that has not changed from a provided next speed limit value
    # in previous resolutions, we keep providing it.
    if next_speed_limit == self._next_speed_limit_prev:
      self._limit_solutions[SpeedLimitResolver.Source.map_data] = next_speed_limit
      self._distance_solutions[SpeedLimitResolver.Source.map_data] = distance_to_speed_limit_ahead
      return

    # Reset tracking
    self._next_speed_limit_prev = 0.

    # Calculated the time needed to adapt to the new limit and the corresponding distance.
    adapt_time = (next_speed_limit - self._v_ego) / _LIMIT_ADAPT_ACC
    adapt_distance = self._v_ego * adapt_time + 0.5 * _LIMIT_ADAPT_ACC * adapt_time**2

    # When we detect we are close enough, we provide the next limit value and track it.
    if distance_to_speed_limit_ahead <= adapt_distance:
      self._limit_solutions[SpeedLimitResolver.Source.map_data] = next_speed_limit
      self._distance_solutions[SpeedLimitResolver.Source.map_data] = distance_to_speed_limit_ahead
      self._next_speed_limit_prev = next_speed_limit
      return

    # Otherwise we just provide the map data speed limit.
    self.distance_to_map_speed_limit = 0.
    self._limit_solutions[SpeedLimitResolver.Source.map_data] = speed_limit
    self._distance_solutions[SpeedLimitResolver.Source.map_data] = 0.

  def _consolidate(self):
    limits = np.array([], dtype=float)
    distances = np.array([], dtype=float)
    sources = np.array([], dtype=int)

    if self._policy == SpeedLimitResolver.Policy.car_state_only or \
       self._policy == SpeedLimitResolver.Policy.car_state_priority or \
       self._policy == SpeedLimitResolver.Policy.combined:
      limits = np.append(limits, self._limit_solutions[SpeedLimitResolver.Source.car_state])
      distances = np.append(distances, self._distance_solutions[SpeedLimitResolver.Source.car_state])
      sources = np.append(sources, SpeedLimitResolver.Source.car_state.value)

    if self._policy == SpeedLimitResolver.Policy.map_data_only or \
       self._policy == SpeedLimitResolver.Policy.map_data_priority or \
       self._policy == SpeedLimitResolver.Policy.combined:
      limits = np.append(limits, self._limit_solutions[SpeedLimitResolver.Source.map_data])
      distances = np.append(distances, self._distance_solutions[SpeedLimitResolver.Source.map_data])
      sources = np.append(sources, SpeedLimitResolver.Source.map_data.value)

    if np.amax(limits) == 0.:
      if self._policy == SpeedLimitResolver.Policy.car_state_priority:
        limits = np.append(limits, self._limit_solutions[SpeedLimitResolver.Source.map_data])
        distances = np.append(distances, self._distance_solutions[SpeedLimitResolver.Source.map_data])
        sources = np.append(sources, SpeedLimitResolver.Source.map_data.value)

      elif self._policy == SpeedLimitResolver.Policy.map_data_priority:
        limits = np.append(limits, self._limit_solutions[SpeedLimitResolver.Source.car_state])
        distances = np.append(distances, self._distance_solutions[SpeedLimitResolver.Source.car_state])
        sources = np.append(sources, SpeedLimitResolver.Source.car_state.value)

    # Get all non-zero values and set the minimum if any, otherwise 0.
    mask = limits > 0.
    limits = limits[mask]
    distances = distances[mask]
    sources = sources[mask]

    if len(limits) > 0:
      min_idx = np.argmin(limits)
      self.speed_limit = limits[min_idx]
      self.distance = distances[min_idx]
      self.source = SpeedLimitResolver.Source(sources[min_idx])
    else:
      self.speed_limit = 0.
      self.distance = 0.
      self.source = SpeedLimitResolver.Source.none

    _debug(f'SL: *** Speed Limit set: {self.speed_limit}, distance: {self.distance}, source: {self.source}')


class SpeedLimitController():
  def __init__(self):
    self._params = Params()
    self._resolver = SpeedLimitResolver()
    self._last_params_update = 0.0
    self._is_metric = self._params.get_bool("IsMetric")
    self._is_enabled = self._params.get_bool("SpeedLimitControl")
    self._delay_increase = self._params.get_bool("SpeedLimitDelayIncrease")
    self._offset_enabled = self._params.get_bool("SpeedLimitPercOffset")
    self._op_enabled = False
    self._acc_limits = [0.0, 0.0]
    self._v_turn = 0.0
    self._v_ego = 0.0
    self._v_offset = 0.0
    self._v_cruise_setpoint = 0.0
    self._v_cruise_setpoint_prev = 0.0
    self._v_cruise_setpoint_changed = False
    self._speed_limit_set = 0.0
    self._speed_limit_set_prev = 0.0
    self._speed_limit_set_change = 0.0
    self._distance_set = 0.0
    self._speed_limit = 0.0
    self._speed_limit_prev = 0.0
    self._speed_limit_changed = False
    self._distance = 0.
    self._source = SpeedLimitResolver.Source.none
    self._last_speed_limit_set_change_ts = 0.0
    self._state = SpeedLimitControlState.inactive
    self._state_prev = SpeedLimitControlState.inactive

  @property
  def v_limit(self):
    return float(self._v_limit) if self.is_active else self._v_cruise_setpoint

  @property
  def acc_limits(self):
    return self._acc_limits

  @property
  def state(self):
    return self._state

  @state.setter
  def state(self, value):
    if value != self._state:
      _debug(f'Speed Limit Controller state: {_description_for_state(value)}')

      if value == SpeedLimitControlState.tempInactive:
        # Make sure speed limit is set to `set` value, this will have the effect
        # of canceling delayed increase limit, if pending.
        self._speed_limit = self._speed_limit_set
        self._distance = self._distance_set
        # Reset previous speed limit to current value as to prevent going out of tempInactive in
        # a single cycle when the speed limit changes at the same time the user has temporarily deactivate it.
        self._speed_limit_prev = self._speed_limit

    self._state = value

  @property
  def is_active(self):
    return self.state > SpeedLimitControlState.tempInactive

  @property
  def speed_limit_offseted(self):
    return self._speed_limit + self.speed_limit_offset

  @property
  def speed_limit_offset(self):
    if self._offset_enabled:
      return interp(self._speed_limit, _LIMIT_PERC_OFFSET_BP, _LIMIT_PERC_OFFSET_V) * self._speed_limit
    return 0.

  @property
  def speed_limit(self):
    return self._speed_limit

  @property
  def distance(self):
    return self._distance

  @property
  def source(self):
    return self._source

  def _update_params(self):
    time = sec_since_boot()
    if time > self._last_params_update + _PARAMS_UPDATE_PERIOD:
      self._is_enabled = self._params.get_bool("SpeedLimitControl")
      self._delay_increase = self._params.get_bool("SpeedLimitDelayIncrease")
      self._offset_enabled = self._params.get_bool("SpeedLimitPercOffset")
      _debug(f'Updated Speed limit params. enabled: {self._is_enabled}, delay increase: {self._delay_increase}')
      self._last_params_update = time

  def _update_calculations(self):
    # Track the time when speed limit set value changes.
    time = sec_since_boot()
    if self._speed_limit_set != self._speed_limit_set_prev:
      self._last_speed_limit_set_change_ts = time

    # Set distance to speed limit to 0 by default. i.e. active speed limit.
    # If the speed limit is ahead, we will update it below.
    self._distance = 0.

    # If not change on limit, we just update the distance to it.
    if self._speed_limit == self._speed_limit_set:
      self._distance = self._distance_set

    # Otherwise update speed limit from the set value.
    # - Imediate when changing from 0 or when updating to a lower speed limit or when increasing
    #   if delay increase is disabled.
    # - After a predefined period of time when increasing speed limit if delayed increase is enabled.
    elif self._speed_limit == 0.0 or self._speed_limit_set < self._speed_limit or not self._delay_increase or \
            time > self._last_speed_limit_set_change_ts + _WAIT_TIME_LIMIT_RISE:
      self._speed_limit = self._speed_limit_set
      self._distance = self._distance_set

    # Update current velocity offset (error)
    self._v_offset = self.speed_limit_offseted - self._v_ego

    # Update change tracking variables
    self._speed_limit_changed = self._speed_limit != self._speed_limit_prev
    self._v_cruise_setpoint_changed = self._v_cruise_setpoint != self._v_cruise_setpoint_prev
    self._speed_limit_set_change = self._speed_limit_set - self._speed_limit_set_prev
    self._speed_limit_prev = self._speed_limit
    self._v_cruise_setpoint_prev = self._v_cruise_setpoint
    self._speed_limit_set_prev = self._speed_limit_set

  def _state_transition(self):
    self._state_prev = self._state

    # In any case, if op is disabled, or speed limit control is disabled
    # or the reported speed limit is 0, deactivate.
    if not self._op_enabled or not self._is_enabled or self._speed_limit == 0:
      self.state = SpeedLimitControlState.inactive
      return

    # In any case, we deactivate the speed limit controller temporarily if the user changes the cruise speed
    #if self._v_cruise_setpoint_changed:
    #  self.state = SpeedLimitControlState.tempInactive
    #  return

    # Check to see if lkas button on HKG was pressed - JPR
    #if not interface.CarInterface.speed_limit:
    #  self.state = SpeedLimitControlState.tempInactive
    #if interface.CarInterface.speed_limit:
     # self.state == SpeedLimitControlState.active

    # inactive
    if self.state == SpeedLimitControlState.inactive:
      # If the limit speed offset is negative (i.e. reduce speed) and lower than threshold
      # we go to adapting state to quickly reduce speed, otherwise we go directly to active
      if self._v_offset < _SPEED_OFFSET_TH:
        self.state = SpeedLimitControlState.adapting
      else:
        self.state = SpeedLimitControlState.active
    # tempInactive
    elif self.state == SpeedLimitControlState.tempInactive:
      # if speed limit changes, transition to inactive,
      # proper active state will be set on next iteration.
      if self._speed_limit_changed:
        self.state = SpeedLimitControlState.inactive
    # adapting
    elif self.state == SpeedLimitControlState.adapting:
      # Go to active once the speed offset is over threshold.
      if self._v_offset >= _SPEED_OFFSET_TH:
        self.state = SpeedLimitControlState.active
    # active
    elif self.state == SpeedLimitControlState.active:
      # Go to adapting if the speed offset goes below threshold.
      if self._v_offset < _SPEED_OFFSET_TH:
        self.state = SpeedLimitControlState.adapting

  def _update_solution(self, sm):
    # Calculate acceleration limits and speed based on state.
    acc_limits = self._acc_limits
    v_limit = self._v_cruise_setpoint

    # inactive or tempInactive state or gas pressed
    if self.state <= SpeedLimitControlState.tempInactive or sm['carState'].gasPressed:
      # Preserve current values
      pass
    # adapting
    elif self.state == SpeedLimitControlState.adapting:
      # When adapting we target the speed limit speed with the adapt acceleration if lower than provided limits.
      v_limit = self.speed_limit_offseted
      acc_limits[0] = min(_LIMIT_ADAPT_ACC, acc_limits[0])
    # active
    elif self.state == SpeedLimitControlState.active:
      v_limit = self.speed_limit_offseted

    # update solution values.
    self._v_limit = v_limit
    self._acc_limits = acc_limits

  def _update_events(self, events):
    if not self.is_active:
      # no event while inactive or deactivating
      return

    if self._state_prev <= SpeedLimitControlState.tempInactive:
      events.add(EventName.speedLimitActive)
    elif self._speed_limit_set_change > 0:
      events.add(EventName.speedLimitIncrease)
    elif self._speed_limit_set_change < 0:
      events.add(EventName.speedLimitDecrease)

  def update(self, enabled, v_ego, sm, v_cruise_setpoint, acc_limits, events=Events()):
    self._op_enabled = enabled
    self._v_ego = v_ego

    self._speed_limit_set, self._distance_set, self._source = self._resolver.resolve(v_ego, self.speed_limit, sm)
    self._v_cruise_setpoint = v_cruise_setpoint
    self._acc_limits = acc_limits

    self._update_params()
    self._update_calculations()
    self._state_transition()
    self._update_solution(sm)
    self._update_events(events)
