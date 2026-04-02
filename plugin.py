"""
<plugin key="Your-Plugin-Key" name="Your Plugin Name" author="Your Name" version="1.0.0" wikilink="https://example.com/wiki" externallink="https://github.com/repo-link">
    <description>
        Describe your plugin here.
    </description>
    <params>
        <param field="Mode1" label="Custom Parameter 1" width="200px" required="true"/>
        <param field="Mode2" label="Custom Parameter 2" width="200px"/>
        <param field="Mode6" label="Debug Level" width="200px">
            <options>
                <option label="None" value="0" default="true" />
                <option label="Basic Debugging" value="62"/>
                <option label="All" value="-1"/>
            </options>
        </param>
    </params>
</plugin>
"""

import Domoticz

class BasePlugin:
    def __init__(self):
        self.param1 = ""
        self.param2 = ""
        self.initialized = False

    def onStart(self):
        Domoticz.Debugging(int(Parameters["Mode6"]))
        Domoticz.Log("Plugin started")
        self.param1 = Parameters["Mode1"]
        self.param2 = Parameters.get("Mode2", "")

        if not self.param1:
            Domoticz.Error("Missing required parameter: Mode1")
            return

        self.initialized = True
        Domoticz.Heartbeat(30)

    def onConnect(self, connection, status, description):
        if status != 0:
            Domoticz.Error(f"Connection failed: {status} - {description}")
        else:
            Domoticz.Log("Connected successfully")

    def onMessage(self, connection, data):
        Domoticz.Log(f"Message received: {data}")

    def onStop(self):
        Domoticz.Log("Plugin stopped")

    def onHeartbeat(self):
        if not self.initialized:
            Domoticz.Error("Plugin not initialized yet")
            return
        Domoticz.Log("Heartbeat called")

global _plugin
_plugin = BasePlugin()

def onStart():
    global _plugin
    _plugin.onStart()

def onStop():
    global _plugin
    _plugin.onStop()

def onConnect(connection, status, description):
    global _plugin
    _plugin.onConnect(connection, status, description)

def onMessage(connection, data):
    global _plugin
    _plugin.onMessage(connection, data)

def onHeartbeat():
    global _plugin
    _plugin.onHeartbeat()
