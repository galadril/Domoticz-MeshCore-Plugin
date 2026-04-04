--[[
    --[[
    MeshCore – dzVents Status Report (demo)
    ========================================
    Example script that sends periodic home-status updates to a
    MeshCore channel. Messages are split into readable themed groups
    and spaced apart so the plugin can serialise each LoRa TX.

    MESSAGE SYNTAX
    --------------
    The MeshCore plugin accepts messages on the Mesh Send device:
      "#General: hello"   → broadcast on the channel named 'General'
      "#0: hello"         → broadcast on channel index 0
      "garden: hello"     → direct message to node named 'garden'
      "hello"             → direct message to first discovered contact

    Set CHANNEL_NAME below to your target channel name.
    The plugin resolves the name to the correct index automatically.
    Channel names are logged by the plugin on startup and visible
    on the MeshCore dashboard.
]]--

-- ═══════════════════════════════════════════════════════════════════
-- CONFIGURATION – adjust these to match your setup
-- ═══════════════════════════════════════════════════════════════════
local CHANNEL_NAME      = 'General'                 -- Channel name to send to (or use a number like '0')
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
        lastQueueSend = { initial = 0 },
    },

    execute = function(dz, triggeredItem)

        -- Safe device lookup: dzVents throws when a device is not found,
        -- so we must use pcall to avoid crashing the whole script.
        local function safeDev(name)
            local ok, d = pcall(dz.devices, name)
            if ok and d then return d end
            return nil
        end

        local sendDev = safeDev(MESHCORE_SEND)
        if (sendDev == nil) then
            dz.log('MeshCore Send device not found! Expected: ' .. MESHCORE_SEND, dz.LOG_ERROR)
            dz.log('Make sure the MeshCore hardware is added and named correctly.', dz.LOG_ERROR)
            return
        end

        -- Helper: send a message to the configured channel
        local function sendMsg(message)
            local payload = '#' .. CHANNEL_NAME .. ': ' .. message
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
            return safeDev(name)
        end

        -- Helper: nil-safe value access
        local function safeVal(val, fallback)
            if (val == nil) then return fallback or 0 end
            return val
        end

        -- ═══════════════════════════════════════════════════════════
        -- CUSTOM EVENT: send next queued message
        -- ═══════════════════════════════════════════════════════════
        if (triggeredItem.isCustomEvent) then
            local q = dz.data.messageQueue
            local now = os.time()
            if (#q > 0 and (now - dz.data.lastQueueSend) >= MSG_DELAY) then
                local msg = table.remove(q, 1)
                sendMsg(msg)
                dz.data.lastQueueSend = now
                dz.data.messageQueue = q
                if (#q > 0) then
                    dz.emitCustomEvent('meshcoreStatusPart')
                end
            elseif (#q > 0) then
                -- Not enough time elapsed; re-trigger to try again later
                dz.emitCustomEvent('meshcoreStatusPart')
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
            table.insert(climate, 'Indoor ' .. round(indoor.temperature) .. 'C, ' .. safeVal(indoor.humidity, 0) .. '%')
        end
        local thermo = dev(DEVICES.thermostat)
        if (thermo) then
            table.insert(climate, 'Thermostat ' .. safeVal(thermo.setPoint, '?') .. 'C')
        end
        if (#climate > 0) then
            table.insert(messages, 'Climate: ' .. table.concat(climate, ' | '))
        end

        -- 2) Weather
        local weather = {}
        local outdoor = dev(DEVICES.tempOutdoor)
        if (outdoor) then
            table.insert(weather, round(outdoor.temperature) .. 'C, ' .. safeVal(outdoor.humidity, 0) .. '%')
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
            dz.data.lastQueueSend = os.time()
            if (#messages > 1) then
                local q = {}
                for i = 2, #messages do
                    table.insert(q, messages[i])
                end
                dz.data.messageQueue = q
                dz.emitCustomEvent('meshcoreStatusPart')
            end
        end

        dz.log('Queued ' .. #messages .. ' status message(s) for channel #' .. CHANNEL_NAME, dz.LOG_INFO)
    end
}
