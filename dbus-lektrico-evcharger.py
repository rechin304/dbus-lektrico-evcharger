#!/usr/bin/env python

# import normal packages
import platform
import logging
import sys
import os
import time
import requests  # for http GET
import configparser  # for config/ini file
import random

# our own packages from victron
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

        self._dbusservice = VeDbusService("{}.http_{:02d}".format(servicename, deviceinstance))
        self._paths = paths

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

        # last update
        self._lastUpdate = 0

        # charging time in float
        self._chargingTime = 0.0

        # add _update function 'timer'
        gobject.timeout_add(250, self._update)  # pause 250ms before the next request

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
                payload = {
                    "src": "VenusOS",
                    "id": random.randint(10000000, 99999999),
                    "method": method,
                    "params": {param_name: value} if param_name else {}
                }           
            #logging.info("URL: %s" % (URL))
        else:
            raise ValueError("AccessType %s is not supported" % (config['DEFAULT']['AccessType']))

        return URL, payload

    def _setLektricoChargerValue(self, method, value, param_name=None):
        URL, payload = self._getLektricoChargerPayloadUrl(method, value, param_name)
        #logging.info("setLektricoChargerValue URL: %s" % (URL))
        logging.info("setLektricoChargerValue Payload: %s" % (payload))
        
        try:
            request_data = requests.post(url=URL, json=payload)
            request_data.raise_for_status()

            json_data = request_data.json()
            # Log the response for debugging
            logging.info("Response from server: %s", json_data)

            # check for Json
            if not json_data:
                raise ValueError("Converting response to JSON failed")

            if 'result' in json_data and json_data['result'] is True:
                return True
            else:
                #logging.warning(f"Lektri.co parameter {parameter} not set to {value}")
                logging.warning(f"Lektri.co parameter {param_name} not set to {value}")
                return False
        except requests.exceptions.RequestException as e:
            logging.warning(f"Error setting Lektri.co parameter {parameter} to {value}: {e}")
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
        logging.info("Setting EV Charger mode to: %s" % mode)
        # Map your mode values to the expected values
        mode_mapping = {0: '2', 1: '3', 2: '1'}
        mapped_mode = mode_mapping.get(mode, '2')  # Default to mode '2' if not found
        try:
            method = 'app_config.set'
            config_key = 'load_balancing_mode'
            config_value = mapped_mode
            
            payload = {
            "src": "HASS",
            "id": random.randint(10000000, 99999999),
            "method": method,
            "params": {"config_key": config_key, "config_value": config_value}
            }
            
            URL = self._setLektricoEMUrl()
            
            logging.info("setLektricoChargerMode Payload: %s" % (payload))
            
            request_data = requests.post(url=URL, json=payload)
            request_data.raise_for_status()

            json_data = request_data.json()
            # Log the response for debugging
            logging.info("Response from server: %s", json_data)
            
            # check for Json
            if not json_data:
                raise ValueError("Converting response to JSON failed")
                
            if 'result' in json_data and json_data['result'] is True:
                return True
            else:
                logging.warning(f"Lektri.co parameter {config_key} not set to {config_value}")
                return False
        
        
        except requests.exceptions.RequestException as e:
            logging.warning(f"Error setting EV Charger mode: {e}")
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
            # get data from LektricoCharger
            data = self._getLektricoChargerData()
            em_data = self._getLektricoEMData()

            if data is not None:
                # send data to DBus
                self._dbusservice['/Ac/L1/Power'] = int(data['instant_power'])
                self._dbusservice['/Ac/Power'] = int(data['instant_power'])
                self._dbusservice['/Ac/Voltage'] = int(data['voltage'])
                self._dbusservice['/Current'] = int(data['current'])
                power_watts = float(data['session_energy'])/1000
                self._dbusservice['/Ac/Energy/Forward'] = power_watts
                #logging.info("Received power data: %s W" % power_watts)
                
                self._dbusservice['/SetCurrent'] = int(data['dynamic_current'])
                self._dbusservice['/MaxCurrent'] = int(data['dynamic_current'])
                self._dbusservice['/ChargingTime'] = int(data['charging_time'])
                mode = 0
                if str(em_data['load_balancing_mode']) == '2':
                    mode = 0
                elif str(em_data['load_balancing_mode']) == '3':
                    mode = 1
                elif str(em_data['load_balancing_mode']) == '1':
                    mode = 2
                self._dbusservice['/Mode'] = mode
                self._dbusservice['/MCU/Temperature'] = int(data['temperature'])
                
                status = 0
                if str(data['charger_state']) == 'A':
                    status = 0
                elif str(data['charger_state']) == 'B':
                    status = 1
                elif str(data['charger_state']) == 'C':
                    status = 2
                elif str(data['charger_state']) == 'D':
                    status = 3
                self._dbusservice['/Status'] = status
                
                
                start_stop_mapping = {0: 0, 1: 0, 2: 1, 3: 0}
                self._dbusservice['/StartStop'] = start_stop_mapping.get(status, 0)
                logging.debug("Start/Stop : %s" % (self._dbusservice['/StartStop']))
                
                logging.debug("Wallbox Consumption (/Ac/Power): %s" % (self._dbusservice['/Ac/Power']))
                logging.debug("Wallbox Forward (/Ac/Energy/Forward): %s" % (self._dbusservice['/Ac/Energy/Forward']))
                logging.debug("---")

                index = self._dbusservice['/UpdateIndex'] + 1
                if index > 255:
                    index = 0
                self._dbusservice['/UpdateIndex'] = index

                self._lastUpdate = time.time()
            else:
                logging.debug("Wallbox is not available")

        except Exception as e:
            logging.critical('Error at %s', '_update', exc_info=e)

        return True

    def _handlechangedvalue(self, path, value):
        logging.info("Someone updated %s to %s" % (path, value))
           
        if path == '/StartStop':
            logging.info("/StartStop value %s" % (value))
            #return self._setLektricoChargerValue('StartStop', value)                                            
            return self._setLektricoChargerValue('charge.start' if value == 1 else 'charge.stop', value)            
            
        elif path == '/SetCurrent':
            logging.info("/SetCurrent value %s" % (value))
            return self._setLektricoChargerValue('dynamic_current.set', value, param_name='dynamic_current')
            
        elif path == '/Mode':
            logging.info("/Mode value %s" % (value))
            return self._setLektricoChargerMode(value)
            
        elif path == '/EnableDisplay':
        # Example: set EnableDisplay to 1 (control enabled)
            return self._setLektricoChargerValue('/EnableDisplay', 1)
        else:
            logging.info("Mapping for evcharger path %s does not exist" % (path))
            return False


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
