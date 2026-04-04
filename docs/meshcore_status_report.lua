--[[
    MeshCore – dzVents Status Report (demo)
    ========================================
    Example script that sends periodic home-status updates to a
    MeshCore channel. Messages are split into readable themed groups
    and spaced apart so the plugin can serialise each LoRa TX.

    MESSAGE SYNTAX
    --------------
    The MeshCore plugin accepts messages on the Mesh Send device:
      "#0: hello"        → broadcast on channel 0
      "#1: hello"        → broadcast on channel 1
      "garden: hello"    → direct message to node named 'garden'
      "hello"            → direct message to first discovered contact
]]--

-- ═══════════════════════════════════════════════════════════════════
-- CONFIGURATION – adjust these to match your setup
-- ═══════════════════════════════════════════════════════════════════
local CHANNEL_INDEX     = 0                         -- Target channel index (check meshcore_channels.json)
local MESHCORE_SEND     = 'MeshCore - Mesh Send'    -- Name of the MeshCore Send device
local MSG_DELAY         = 45                        -- Seconds between queued messages

-- Device names – replace with your own Domoticz device names
local DEVICES = {
    tempIndoor      = 'Temperature - Living Room',  -- Temp + Humidity sensor
    tempOutdoor     = 'Temperature - Outside',      -- Temp + Humidity sensor
    thermostat      = 'Thermostat',                 -- Setpoint device
    power           = 'Power',                      -- P1 Smart Meter (energy)
    solar           = 'Solar Power',                -- kWh device
    gas             = 'Gas',                         -- P1 Smart Meter (gas)
    presence        = 'Presence',                   -- Selector switch (home/away/etc)
}
-- ═══════════════════════════════════════════════════════════════════

return {
    active = true,

    on = {
        timer = {
            'every 30 minutes',
        },
        devices = {
            DEVICES.presence,
        },
        customEvents = {
            'meshcoreStatusPart',
        },
    },

    logging = {
        level = domoticz.LOG_INFO,
        marker = 'MeshCoreReport',
    },

    data = {
        lastPresence = { initial = '' },
        messageQueue = { initial = {} },
        skipNext     = { initial = false },
    },

    execute = function(dz, triggeredItem)

        local sendDev = dz.devices(MESHCORE_SEND)
        if (sendDev == nil) then
            dz.log('MeshCore Send device not found!', dz.LOG_ERROR)
            return
        end

        -- Helper: send a message to the configured channel
        local function sendMsg(message)
            local payload = '#' .. CHANNEL_INDEX .. ': ' .. message
            dz.log('Sending: ' .. payload, dz.LOG_INFO)
            sendDev.updateText(payload)
        end

        -- Helper: round a number
        local function round(num, dec)
            if (num == nil) then return 0 end
            local m = 10 ^ (dec or 1)
            return math.floor(num * m + 0.5) / m
        end

        -- Helper: safe device lookup
        local function dev(name)
            return dz.devices(name)
        end

        -- ═══════════════════════════════════════════════════════════
        -- CUSTOM EVENT: send next queued message
        -- ═══════════════════════════════════════════════════════════
        if (triggeredItem.isCustomEvent) then
            local q = dz.data.messageQueue
            if (#q > 0) then
                local msg = table.remove(q, 1)
                sendMsg(msg)
                if (#q > 0) then
                    dz.emitCustomEvent('meshcoreStatusPart').afterSec(MSG_DELAY)
                end
                dz.data.messageQueue = q
            end
            return
        end

        -- ═══════════════════════════════════════════════════════════
        -- PRESENCE CHANGE (immediate alert)
        -- ═══════════════════════════════════════════════════════════
        if (triggeredItem.isDevice and triggeredItem.name == DEVICES.presence) then
            local status = triggeredItem.levelName or triggeredItem.state
            if (status ~= dz.data.lastPresence) then
                dz.data.lastPresence = status
                sendMsg('Presence: ' .. status)
            end
            return
        end

        -- ═══════════════════════════════════════════════════════════
        -- HOURLY STATUS REPORT
        -- Timer fires every 30 min; skip alternates to get ~60 min
        -- ═══════════════════════════════════════════════════════════
        if (dz.data.skipNext) then
            dz.data.skipNext = false
            return
        end
        dz.data.skipNext = true

        local messages = {}

        -- 1) Climate
        local climate = {}
        local indoor = dev(DEVICES.tempIndoor)
        if (indoor) then
            table.insert(climate, 'Indoor ' .. round(indoor.temperature) .. 'C, ' .. indoor.humidity .. '%')
        end
        local thermo = dev(DEVICES.thermostat)
        if (thermo) then
            table.insert(climate, 'Thermostat ' .. thermo.setPoint .. 'C')
        end
        if (#climate > 0) then
            table.insert(messages, 'Climate: ' .. table.concat(climate, ' | '))
        end

        -- 2) Weather
        local weather = {}
        local outdoor = dev(DEVICES.tempOutdoor)
        if (outdoor) then
            table.insert(weather, round(outdoor.temperature) .. 'C, ' .. outdoor.humidity .. '%')
        end
        if (#weather > 0) then
            table.insert(messages, 'Weather: ' .. table.concat(weather, ' | '))
        end

        -- 3) Energy
        local energy = {}
        local pw = dev(DEVICES.power)
        if (pw) then
            local use = pw.usage or 0
            local del = pw.usageDelivered or 0
            if (del > 0) then
                table.insert(energy, 'Delivery ' .. del .. 'W')
            else
                table.insert(energy, 'Usage ' .. use .. 'W')
            end
        end
        local sol = dev(DEVICES.solar)
        if (sol) then
            local w = sol.WhActual or 0
            if (w > 0) then
                table.insert(energy, 'Solar ' .. w .. 'W')
            end
        end
        local gas = dev(DEVICES.gas)
        if (gas) then
            table.insert(energy, 'Gas today ' .. gas.counterToday)
        end
        if (#energy > 0) then
            table.insert(messages, 'Energy: ' .. table.concat(energy, ' | '))
        end

        -- Send first message now, queue the rest with delays
        if (#messages > 0) then
            sendMsg(messages[1])
            if (#messages > 1) then
                local q = {}
                for i = 2, #messages do
                    table.insert(q, messages[i])
                end
                dz.data.messageQueue = q
                dz.emitCustomEvent('meshcoreStatusPart').afterSec(MSG_DELAY)
            end
        end

        dz.log('Queued ' .. #messages .. ' status message(s) for channel #' .. CHANNEL_INDEX, dz.LOG_INFO)
    end
}
