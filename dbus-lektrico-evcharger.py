#!/usr/bin/env python

import platform
import logging
import sys
import os
import time
import requests
import configparser
import random
import dbus
import traceback

# Victron packages
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '/opt/victronenergy/dbus-systemcalc-py/ext/velib_python'))
from vedbus import VeDbusService

if sys.version_info.major == 2:
    import gobject
else:
    from gi.repository import GLib as gobject


class DbusLektricoService:
    def __init__(self, servicename, paths, productname='Lektri.co 1p7k', connection='Lektri.co HTTP JSON service'):
        config = self._getConfig()
        deviceinstance = int(config['DEFAULT']['Deviceinstance'])
        hardwareVersion = int(config['DEFAULT']['HardwareVersion'])

        self._dbusservice = VeDbusService("{}.http_{:02d}".format(servicename, deviceinstance), register=False)
        self._paths = paths
        self._updating = False  # Flag to prevent feedback loops during state updates
        self._last_start_stop_from_charger = None  # Track last value read from charger
        self._last_set_current_from_charger = None  # Track last current value read from charger
        self._last_mode_from_charger = None  # Track last mode value read from charger
        self._restarting_after_change = False  # Flag to prevent stop commands during auto-restart
        self._last_user_start_stop_command = None  # Track last command sent by user
        self._last_user_start_stop_time = 0  # Track when last user command was sent

        logging.debug("%s /DeviceInstance = %d" % (servicename, deviceinstance))

        paths_wo_unit = [
            '/Status'
        ]

        # get data from go-eCharger
        data = self._getLektricoChargerData()
        chargerconfig = self._getLektricoChargerConfig()

        # Create the management objects, as specified in the ccgx dbus-api document
        self._dbusservice.add_path('/Mgmt/ProcessName', __file__)
        self._dbusservice.add_path('/Mgmt/ProcessVersion', 'Unkown version, and running on Python ' +
                                   platform.python_version())
        self._dbusservice.add_path('/Mgmt/Connection', connection)

        # Create the mandatory objects
        self._dbusservice.add_path('/DeviceInstance', deviceinstance)
        self._dbusservice.add_path('/ProductId', 0xFFFF)
        self._dbusservice.add_path('/ProductName', productname)
        self._dbusservice.add_path('/CustomName', productname)
        if data:
            self._dbusservice.add_path('/FirmwareVersion', int(data['fw_version'].replace('.', '')))
            self._dbusservice.add_path('/Serial', chargerconfig['serial_number'])
        self._dbusservice.add_path('/HardwareVersion', hardwareVersion)
        self._dbusservice.add_path('/Connected', 1)
        self._dbusservice.add_path('/UpdateIndex', 0)

        # add paths without units
        for path in paths_wo_unit:
            self._dbusservice.add_path(path, None)

        # add path values to dbus
        for path, settings in self._paths.items():
            self._dbusservice.add_path(
                path, settings['initial'], gettextcallback=settings['textformat'], writeable=True,
                onchangecallback=self._handlechangedvalue)

        # Register the service on D-Bus after adding all paths
        self._dbusservice.register()

        # last update
        self._lastUpdate = 0

        # add _update function 'timer'
        gobject.timeout_add(250, self._update)

        # add _signOfLife 'timer' to get feedback in log every 5 minutes
        gobject.timeout_add(self._getSignOfLifeInterval() * 60 * 1000, self._signOfLife)

    def _getConfig(self):
        config = configparser.ConfigParser()
        config.read("%s/config.ini" % (os.path.dirname(os.path.realpath(__file__))))
        return config

    def _getSignOfLifeInterval(self):
        config = self._getConfig()
        value = config['DEFAULT']['SignOfLifeLog']

        if not value:
            value = 0

        return int(value)

    def _getLektricoChargerStatusUrl(self):
        config = self._getConfig()
        accessType = config['DEFAULT']['AccessType']

        if accessType == 'OnPremise':
            URL = "http://%s/rpc/charger_info.get" % (config['ONPREMISE']['Host'])
        else:
            raise ValueError("AccessType %s is not supported" % (config['DEFAULT']['AccessType']))

        return URL

    def _getLektricoChargerConfigUrl(self):
        config = self._getConfig()
        accessType = config['DEFAULT']['AccessType']

        if accessType == 'OnPremise':
            URL = "http://%s/rpc/charger_config.get" % (config['ONPREMISE']['Host'])
        else:
            raise ValueError("AccessType %s is not supported" % (config['DEFAULT']['AccessType']))

        return URL

    def _getLektricoChargerPayloadUrl(self, method, value, param_name=None):
        config = self._getConfig()
        accessType = config['DEFAULT']['AccessType']

        if accessType == 'OnPremise':
            URL = "http://%s/rpc" % (config['ONPREMISE']['Host'])
            
            if method == 'charge.start' or method == 'charge.stop':
                payload = {
                    "src": "VenusOS",
                    "id": random.randint(10000000, 99999999),
                    "method": method,
                    "params": {"tag": "Victron"}
                }
                
            else:
                # Ensure numeric values are integers for Lektrico API
                if param_name == 'dynamic_current' and isinstance(value, (int, float)):
                    value = int(value)
                
                payload = {
                    "src": "VenusOS",
                    "id": random.randint(10000000, 99999999),
                    "method": method,
                    "params": {param_name: value} if param_name else {}
                }
        else:
            raise ValueError("AccessType %s is not supported" % (config['DEFAULT']['AccessType']))

        return URL, payload

    def _setLektricoChargerValue(self, method, value, param_name=None):
        URL, payload = self._getLektricoChargerPayloadUrl(method, value, param_name)
        logging.debug("Sending to Lektrico: %s" % method)
        
        try:
            request_data = requests.post(url=URL, json=payload)
            request_data.raise_for_status()
            json_data = request_data.json()

            if not json_data:
                raise ValueError("Converting response to JSON failed")

            if 'result' in json_data and json_data['result'] is True:
                return True
            else:
                logging.warning(f"Lektrico parameter {param_name} not set to {value}")
                return False
        except requests.exceptions.RequestException as e:
            logging.warning(f"Error setting Lektrico parameter {param_name} to {value}: {e}")
            return False
            
    def _getLektricoEMStatusUrl(self):
        config = self._getConfig()
        accessType = config['DEFAULT']['AccessType']

        if accessType == 'OnPremise':
            URL = "http://%s/rpc/app_config.get" % (config['ONPREMISE']['EM_Host'])
        else:
            raise ValueError("AccessType %s is not supported" % (config['DEFAULT']['AccessType']))

        return URL
        
    def _setLektricoEMUrl(self):
        config = self._getConfig()
        accessType = config['DEFAULT']['AccessType']

        if accessType == 'OnPremise':
            URL = "http://%s/rpc" % (config['ONPREMISE']['EM_Host'])
        else:
            raise ValueError("AccessType %s is not supported" % (config['DEFAULT']['AccessType']))

        return URL
        
    def _getLektricoEMData(self):
        URL = self._getLektricoEMStatusUrl()
        try:
            request_data = requests.get(url=URL, timeout=5)
        except Exception:
            return None

        # check for response
        if not request_data:
            raise ConnectionError("No response from Lektri.co - %s" % (URL))

        json_data = request_data.json()

        # check for Json
        if not json_data:
            raise ValueError("Converting response to JSON failed")

        return json_data
        
    def _setLektricoChargerMode(self, mode):
        # Map Victron mode values to Lektrico values
        # Lektrico modes: 1=Green, 2=Power, 3=Hybrid
        mode_mapping = {0: 1, 1: 3, 2: 2}  # Manual→Green, Auto→Hybrid, Scheduled→Power
        mapped_mode = mode_mapping.get(mode, 2)
        
        logging.info("Setting charger mode to %s (Lektrico mode: %s)" % (mode, mapped_mode))
        
        # Remember if charger was charging before mode change
        was_charging = self._dbusservice['/StartStop'] == 1
        
        try:
            payload = {
                "src": "HASS",
                "id": random.randint(10000000, 99999999),
                "method": 'app_config.set',
                "params": {"config_key": 'load_balancing_mode', "config_value": mapped_mode}
            }
            
            URL = self._setLektricoEMUrl()
            request_data = requests.post(url=URL, json=payload)
            request_data.raise_for_status()
            json_data = request_data.json()
            
            if not json_data:
                raise ValueError("Converting response to JSON failed")
                
            if 'result' in json_data and json_data['result'] is True:
                time.sleep(1)  # Wait for EM to update
                
                # If charger was charging before mode change, restart it
                if was_charging:
                    logging.info("Restarting charge after mode change")
                    restart_result = self._setLektricoChargerValue('charge.start', 1)
                    if restart_result:
                        self._last_start_stop_from_charger = 1
                    else:
                        logging.warning("Failed to resume charging after mode change")
                    self._restarting_after_change = False
                
                return True
            else:
                logging.warning(f"Mode not set to {mapped_mode}")
                return False
        
        except requests.exceptions.RequestException as e:
            logging.warning(f"Error setting mode: {e}")
            return False
        
    def _getLektricoChargerData(self):
        URL = self._getLektricoChargerStatusUrl()
        try:
            request_data = requests.get(url=URL, timeout=5)
        except Exception:
            return None

        # check for response
        if not request_data:
            raise ConnectionError("No response from Lektri.co - %s" % (URL))

        json_data = request_data.json()

        # check for Json
        if not json_data:
            raise ValueError("Converting response to JSON failed")

        return json_data

    def _getLektricoChargerConfig(self):
        URL = self._getLektricoChargerConfigUrl()
        try:
            request_data = requests.get(url=URL, timeout=5)
        except Exception:
            return None

        # check for response
        if not request_data:
            raise ConnectionError("No response from Lektri.co - %s" % (URL))

        json_data = request_data.json()

        # check for Json
        if not json_data:
            raise ValueError("Converting response to JSON failed")

        return json_data

    def _signOfLife(self):
        logging.info("--- Start: sign of life ---")
        logging.info("Last _update() call: %s" % (self._lastUpdate))
        logging.info("Last '/Ac/Power': %s" % (self._dbusservice['/Ac/Power']))
        logging.info("--- End: sign of life ---")
        return True

    def _update(self):
        try:
            data = self._getLektricoChargerData()
            em_data = self._getLektricoEMData()

            if data is not None:
                self._updating = True
                
                # Update power and energy values
                self._dbusservice['/Ac/L1/Power'] = int(data['instant_power'])
                self._dbusservice['/Ac/Power'] = int(data['instant_power'])
                self._dbusservice['/Ac/Voltage'] = int(data['voltage'])
                self._dbusservice['/Current'] = int(data['current'])
                self._dbusservice['/Ac/Energy/Forward'] = float(data['session_energy'])/1000
                
                # Update current - log only if changed
                charger_dynamic_current = int(data['dynamic_current'])
                if self._last_set_current_from_charger is not None and charger_dynamic_current != self._last_set_current_from_charger:
                    logging.info("Current changed: %d → %dA" % (self._last_set_current_from_charger, charger_dynamic_current))
                
                self._last_set_current_from_charger = charger_dynamic_current
                self._dbusservice['/SetCurrent'] = charger_dynamic_current
                self._dbusservice['/MaxCurrent'] = charger_dynamic_current
                self._dbusservice['/ChargingTime'] = int(data['charging_time'])
                
                # Map Lektrico mode to Victron mode
                mode_mapping = {'3': 1, '1': 0, '2': 2}  # Green→Auto, Power→Manual, Hybrid→Scheduled
                mode = mode_mapping.get(str(em_data['load_balancing_mode']), 0)
                
                # Log only mode changes
                if self._last_mode_from_charger is not None and mode != self._last_mode_from_charger:
                    logging.info("Mode changed: %d → %d" % (self._last_mode_from_charger, mode))
                
                self._last_mode_from_charger = mode
                self._dbusservice['/Mode'] = mode
                self._dbusservice['/MCU/Temperature'] = int(data['temperature'])
                
                # Map charger state to status
                state_mapping = {'A': 0, 'B': 1, 'C': 2, 'D': 3}
                status = state_mapping.get(str(data['charger_state']), 0)
                self._dbusservice['/Status'] = status
                
                # Map status to start/stop (only C=charging means started)
                new_start_stop = 1 if status == 2 else 0
                
                # Log only state changes
                if new_start_stop != self._dbusservice['/StartStop']:
                    logging.info("Charger state changed: %s → /StartStop=%d" % (data['charger_state'], new_start_stop))
                
                self._last_start_stop_from_charger = new_start_stop
                self._dbusservice['/StartStop'] = new_start_stop

                # Update index
                index = (self._dbusservice['/UpdateIndex'] + 1) % 256
                self._dbusservice['/UpdateIndex'] = index
                self._lastUpdate = time.time()
                self._updating = False
            else:
                logging.debug("Charger not available")
                self._updating = False

        except Exception as e:
            logging.critical('Error in _update', exc_info=e)
            self._updating = False

        return True

    def _handlechangedvalue(self, path, value):
        # Ignore changes during state updates to prevent feedback loops
        if self._updating or self._restarting_after_change:
            logging.debug("Ignoring %s change during update/restart" % path)
            return True
        
        # Try to identify the D-Bus sender (for debugging external control)
        sender_info = self._get_dbus_sender()
        if sender_info:
            logging.info("D-Bus change: %s=%s from %s" % (path, value, sender_info))
        
        # For StartStop, ignore if this is the same value we just read from the charger
        if path == '/StartStop' and self._last_start_stop_from_charger is not None:
            if value == self._last_start_stop_from_charger:
                logging.debug("Ignoring /StartStop - matches charger state")
                return True
            else:
                # Check if this is a delayed callback from recent user command
                time_since_last_command = time.time() - self._last_user_start_stop_time
                if self._last_user_start_stop_command == value and time_since_last_command < 5.0:
                    logging.debug("Ignoring /StartStop - delayed callback")
                    self._last_start_stop_from_charger = value
                    return True
                
                logging.info("/StartStop changed: %s → %s%s" % 
                            (self._last_start_stop_from_charger, value, 
                             " (external)" if sender_info else ""))
                self._last_start_stop_from_charger = None
        
        # For SetCurrent, ignore if matches charger state
        if path == '/SetCurrent' and self._last_set_current_from_charger is not None:
            if value == self._last_set_current_from_charger:
                logging.debug("Ignoring /SetCurrent - matches charger state")
                return True
            else:
                logging.info("/SetCurrent changed: %s → %sA" % (self._last_set_current_from_charger, value))
                self._last_set_current_from_charger = None
        
        # For Mode, ignore if matches charger state
        if path == '/Mode' and self._last_mode_from_charger is not None:
            if value == self._last_mode_from_charger:
                logging.debug("Ignoring /Mode - matches charger state")
                return True
            else:
                logging.info("/Mode changed: %s → %s" % (self._last_mode_from_charger, value))
                self._last_mode_from_charger = None
           
        if path == '/StartStop':
            self._last_user_start_stop_command = value
            self._last_user_start_stop_time = time.time()
            return self._setLektricoChargerValue('charge.start' if value == 1 else 'charge.stop', value)            
            
        elif path == '/SetCurrent':
            was_charging = self._dbusservice['/StartStop'] == 1
            if was_charging:
                self._restarting_after_change = True
            
            result = self._setLektricoChargerValue('dynamic_current.set', value, param_name='dynamic_current')
            if result:
                self._last_set_current_from_charger = value
                
                if was_charging:
                    time.sleep(0.5)
                    restart_result = self._setLektricoChargerValue('charge.start', 1)
                    if restart_result:
                        self._last_start_stop_from_charger = 1
                    self._restarting_after_change = False
            else:
                self._restarting_after_change = False
                
            return result
            
        elif path == '/Mode':
            was_charging = self._dbusservice['/StartStop'] == 1
            if was_charging:
                self._restarting_after_change = True
            
            result = self._setLektricoChargerMode(value)
            if result:
                self._last_mode_from_charger = value
            
            self._restarting_after_change = False
            return result
            
        elif path == '/EnableDisplay':
            return self._setLektricoChargerValue('/EnableDisplay', 1)
        else:
            logging.warning("Unknown path: %s" % path)
            return False

    def _get_dbus_sender(self):
        """Get D-Bus sender information for debugging external control"""
        try:
            msg = dbus.lowlevel.get_calling_message()
            if msg:
                sender = msg.get_sender()
                bus = dbus.SystemBus()
                pid = bus.get_unix_process_id(sender)
                
                # Try to find service name
                dbus_obj = bus.get_object('org.freedesktop.DBus', '/org/freedesktop/DBus')
                dbus_iface = dbus.Interface(dbus_obj, 'org.freedesktop.DBus')
                names = dbus_iface.ListNames()
                for name in names:
                    if not name.startswith(':'):
                        try:
                            owner = dbus_iface.GetNameOwner(name)
                            if owner == sender:
                                return "%s (PID:%s)" % (name, pid)
                        except:
                            pass
                return "PID:%s" % pid
        except Exception as e:
            logging.debug("Could not identify D-Bus sender: %s" % e)
        return None


def main():
    # configure logging
    logging.basicConfig(format='%(asctime)s,%(msecs)d %(name)s %(levelname)s %(message)s',
                        datefmt='%Y-%m-%d %H:%M:%S',
                        level=logging.INFO,
                        handlers=[
                            logging.FileHandler("%s/current.log" % (os.path.dirname(os.path.realpath(__file__)))),
                            logging.StreamHandler()
                        ])

    try:
        logging.info("Start")

        from dbus.mainloop.glib import DBusGMainLoop
        # Have a mainloop, so we can send/receive asynchronous calls to and from dbus
        DBusGMainLoop(set_as_default=True)

        _kwh = lambda p, v: (str(round(v, 2)) + 'kWh')
        _a = lambda p, v: (str(round(v, 1)) + 'A')
        _w = lambda p, v: (str(round(v, 1)) + 'W')
        _v = lambda p, v: (str(round(v, 1)) + 'V')
        _degC = lambda p, v: (str(v) + '°C')
        _s = lambda p, v: (str(v) + 's')

        pvac_output = DbusLektricoService(
            servicename='com.victronenergy.evcharger',
            paths={
                '/Ac/Power': {'initial': 0, 'textformat': _w},
                '/Ac/L1/Power': {'initial': 0, 'textformat': _w},
                '/Ac/L2/Power': {'initial': 0, 'textformat': _w},
                '/Ac/L3/Power': {'initial': 0, 'textformat': _w},
                '/Ac/Energy/Forward': {'initial': 0, 'textformat': _kwh},
                '/ChargingTime': {'initial': 0, 'textformat': _s},

                '/Ac/Voltage': {'initial': 0, 'textformat': _v},
                '/Current': {'initial': 0, 'textformat': _a},
                '/SetCurrent': {'initial': 0, 'textformat': _a},
                '/MaxCurrent': {'initial': 0, 'textformat': _a},
                '/MCU/Temperature': {'initial': 0, 'textformat': _degC},
                '/StartStop': {'initial': 0, 'textformat': lambda p, v: (str(v))},
                '/Mode': {'initial': 0, 'textformat': lambda p, v: (str(v))}
            }
        )

        logging.info('Connected to dbus, and switching over to gobject.MainLoop() (= event based)')
        mainloop = gobject.MainLoop()
        mainloop.run()
    except Exception as e:
        logging.critical('Error at %s', 'main', exc_info=e)


if __name__ == "__main__":
    main()
