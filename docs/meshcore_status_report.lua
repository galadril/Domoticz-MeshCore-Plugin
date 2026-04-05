--[[
    MeshCore – dzVents Status Report (demo)
    ========================================
    Example script that sends periodic home-status updates to a
    MeshCore channel. Messages are split into readable themed groups
    and spaced apart so the plugin can serialise each LoRa TX.

    The script runs every minute. On the hour it builds themed
    message groups and sends one per minute. Once all groups are
    sent the script idles until the next hour.

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
            'every minute',
        },
        devices = {
            DEVICES.presence,
        },
    },

    logging = {
        level = domoticz.LOG_INFO,
        marker = 'MeshCoreReport',
    },

    data = {
        lastPresence = { initial = '' },
        pendingMsgs  = { initial = {} },   -- queued messages, one sent per minute
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
        -- TIMER: drain one queued message per minute
        -- ═══════════════════════════════════════════════════════════
        local q = dz.data.pendingMsgs

        if (#q > 0) then
            sendMsg(table.remove(q, 1))
            dz.data.pendingMsgs = q
            return
        end

        -- ═══════════════════════════════════════════════════════════
        -- HOURLY: build new report on the hour (minute == 0)
        -- ═══════════════════════════════════════════════════════════
        local minute = tonumber(os.date('%M'))
        if (minute ~= 0) then
            return
        end

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

        -- Send first message now, queue the rest (one per minute)
        if (#messages > 0) then
            sendMsg(table.remove(messages, 1))
            dz.data.pendingMsgs = messages
        end

        dz.log('Built ' .. (#messages + 1) .. ' status message(s) for #' .. CHANNEL_NAME, dz.LOG_INFO)
    end
}
