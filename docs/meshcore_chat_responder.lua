    --[[
    --[[
    MeshCore – dzVents Chat Responder (demo)
    =========================================
    Listens for incoming MeshCore messages and responds to commands
    with live Domoticz device status information.

    The script watches the MeshCore Inbox text device. When a message
    arrives on the configured channel containing a known command, it
    queries Domoticz devices and sends the result back on the same
    channel via the MeshCore Send device.

    Multi-line responses (e.g. full status) are queued and sent one
    per minute to avoid LoRa TX overlap.

    The inbox format is: [ChannelName|sender] text  or  [P|sender] text
    Commands must start with a prefix character (default: !) so the
    bot only reacts to explicit requests and never to its own replies.

    SUPPORTED COMMANDS (case-insensitive)
    ??????????????????????????????????????
      !help             ? list available commands
      !status           ? full summary (climate, weather, energy, house)
      !climate          ? indoor climate + heating status
      !weather          ? outdoor weather conditions
      !energy           ? power, solar, battery, gas
      !home             ? water usage, presence
      !device <name>    ? query any Domoticz device by name
      !switches         ? list all switches and their states
      !temp             ? all temperature sensors

    CONFIGURATION
    ?????????????
    Set CHANNEL_NAME to the channel you want to monitor and reply on.
    Set CMD_PREFIX to the prefix character for commands (default: !).
    Adjust device names in the command handlers to match your setup.

    MESSAGE SYNTAX
    ??????????????
    Same as the status report script:
      "#General: hello"   ? broadcast on the channel named 'General'
      "#0: hello"         ? broadcast on channel index 0
]]--

-- ???????????????????????????????????????????????????????????????????
-- CONFIGURATION – adjust these to match your setup
-- ???????????????????????????????????????????????????????????????????
local CHANNEL_NAME      = 'General'                  -- Channel to monitor AND reply on
local MESHCORE_INBOX    = 'MeshCore - Mesh Inbox'    -- Name of the MeshCore Inbox device
local MESHCORE_SEND     = 'MeshCore - Mesh Send'     -- Name of the MeshCore Send device
local CMD_PREFIX        = '!'                        -- Command prefix (messages without this are ignored)

-- Device names – replace with your own Domoticz device names
local DEVICES = {
    tempIndoor      = 'Temperature - Living Room',
    tempBathroom    = 'Temperature - Bathroom',
    tempOutdoor     = 'Temperature - Outside',
    thermostat      = 'Thermostat',
    heatpump        = 'Heat Pump Status',
    ventilation     = 'Ventilation',
    power           = 'Power',
    solar           = 'Solar Power',
    homeBattery     = 'Home Battery',
    gas             = 'Gas',
    water           = 'Water Meter',
    presence        = 'Presence',
    wind            = 'Wind',
    rain            = 'Rain',
}
-- ???????????????????????????????????????????????????????????????????

return {
    active = true,

    on = {
        timer = {
            'every minute',
        },
        devices = {
            MESHCORE_INBOX,
        },
    },

    logging = {
        level = domoticz.LOG_INFO,
        marker = 'MeshChat',
    },

    data = {
        pendingReplies = { initial = {} },
        lastHandled    = { initial = '' },
    },

    execute = function(dz, triggeredItem)

        -- ?????????????????????????????????????????????????????????
        -- Helpers
        -- ?????????????????????????????????????????????????????????

        local function safeDev(name)
            local ok, d = pcall(dz.devices, name)
            if ok and d then return d end
            return nil
        end

        local sendDev = safeDev(MESHCORE_SEND)
        if (sendDev == nil) then
            dz.log('MeshCore Send device not found: ' .. MESHCORE_SEND, dz.LOG_ERROR)
            return
        end

        local function sendReply(message)
            local payload = '#' .. CHANNEL_NAME .. ': ' .. message
            dz.log('Replying: ' .. payload, dz.LOG_INFO)
            sendDev.updateText(payload)
        end

        local function round(num, dec)
            if (num == nil) then return 0 end
            local m = 10 ^ (dec or 1)
            return math.floor(num * m + 0.5) / m
        end

        local function safeVal(val, fallback)
            if (val == nil) then return fallback or 0 end
            return val
        end

        local function lower(s)
            if (s == nil) then return '' end
            return string.lower(s)
        end

        local function trim(s)
            if (s == nil) then return '' end
            return s:match('^%s*(.-)%s*$')
        end

        -- ?????????????????????????????????????????????????????????
        -- Command handlers – adjust device names to match yours
        -- ?????????????????????????????????????????????????????????

        local function cmdHelp()
            return 'Commands: ' .. CMD_PREFIX .. 'status | ' .. CMD_PREFIX .. 'climate | ' .. CMD_PREFIX .. 'weather | ' .. CMD_PREFIX .. 'energy | ' .. CMD_PREFIX .. 'home | ' .. CMD_PREFIX .. 'device <name> | ' .. CMD_PREFIX .. 'switches | ' .. CMD_PREFIX .. 'temp | ' .. CMD_PREFIX .. 'help'
        end

        local function cmdClimate()
            local parts = {}
            local indoor = safeDev(DEVICES.tempIndoor)
            if (indoor) then
                table.insert(parts, 'Indoor ' .. round(indoor.temperature) .. 'C, ' .. safeVal(indoor.humidity, 0) .. '%')
            end
            local bath = safeDev(DEVICES.tempBathroom)
            if (bath) then
                table.insert(parts, 'Bathroom ' .. round(bath.temperature) .. 'C, ' .. safeVal(bath.humidity, 0) .. '%')
            end
            local th = safeDev(DEVICES.thermostat)
            if (th) then
                table.insert(parts, 'Thermostat ' .. safeVal(th.setPoint, '?') .. 'C')
            end
            local hp = safeDev(DEVICES.heatpump)
            if (hp) then
                table.insert(parts, 'Heat pump: ' .. hp.text)
            end
            local vent = safeDev(DEVICES.ventilation)
            if (vent) then
                table.insert(parts, 'Ventilation: ' .. (vent.levelName or vent.state))
            end
            if (#parts == 0) then return 'No climate devices found' end
            return 'Climate: ' .. table.concat(parts, ' | ')
        end

        local function cmdWeather()
            local parts = {}
            local bt = safeDev(DEVICES.tempOutdoor)
            if (bt) then
                table.insert(parts, round(bt.temperature) .. 'C, ' .. safeVal(bt.humidity, 0) .. '%')
            end
            local wind = safeDev(DEVICES.wind)
            if (wind) then
                table.insert(parts, 'Wind ' .. safeVal(wind.directionString, '?') .. ' ' .. round(wind.speed) .. ' m/s')
            end
            local rain = safeDev(DEVICES.rain)
            if (rain and tonumber(rain.rain or 0) > 0) then
                table.insert(parts, 'Rain ' .. rain.rain .. ' mm')
            end
            if (#parts == 0) then return 'No weather devices found' end
            return 'Weather: ' .. table.concat(parts, ' | ')
        end

        local function cmdEnergy()
            local parts = {}
            local pw = safeDev(DEVICES.power)
            if (pw) then
                local use = pw.usage or 0
                local del = pw.usageDelivered or 0
                if (del > 0) then
                    table.insert(parts, 'Delivering ' .. del .. 'W')
                else
                    table.insert(parts, 'Using ' .. use .. 'W')
                end
            end
            local sol = safeDev(DEVICES.solar)
            if (sol) then
                table.insert(parts, 'Solar ' .. (sol.WhActual or 0) .. 'W')
            end
            local hbat = safeDev(DEVICES.homeBattery)
            if (hbat) then
                table.insert(parts, 'Battery ' .. hbat.percentage .. '%')
            end
            local gas = safeDev(DEVICES.gas)
            if (gas) then
                table.insert(parts, 'Gas today ' .. gas.counterToday)
            end
            if (#parts == 0) then return 'No energy devices found' end
            return 'Energy: ' .. table.concat(parts, ' | ')
        end

        local function cmdHome()
            local parts = {}
            local water = safeDev(DEVICES.water)
            if (water) then
                table.insert(parts, 'Water today ' .. water.counterToday)
            end
            local pres = safeDev(DEVICES.presence)
            if (pres) then
                table.insert(parts, 'Presence: ' .. (pres.levelName or pres.state))
            end
            if (#parts == 0) then return 'No home devices found' end
            return 'Home: ' .. table.concat(parts, ' | ')
        end

        local function cmdDevice(name)
            if (name == nil or name == '') then
                return 'Usage: device <name>'
            end
            local d = safeDev(name)
            if (d == nil) then
                return 'Device "' .. name .. '" not found'
            end
            local info = d.name .. ': '
            if (d.temperature ~= nil) then
                info = info .. round(d.temperature) .. 'C'
                if (d.humidity ~= nil) then
                    info = info .. ', ' .. d.humidity .. '%'
                end
            elseif (d.percentage ~= nil) then
                info = info .. d.percentage .. '%'
            elseif (d.setPoint ~= nil) then
                info = info .. d.setPoint .. 'C'
            elseif (d.levelName ~= nil and d.levelName ~= '') then
                info = info .. d.levelName
            elseif (d.state ~= nil) then
                info = info .. d.state
            elseif (d.text ~= nil and d.text ~= '') then
                info = info .. d.text
            else
                info = info .. (d.sValue or 'unknown')
            end
            if (d.lastUpdate ~= nil) then
                info = info .. ' (updated: ' .. d.lastUpdate.raw .. ')'
            end
            return info
        end

        local function cmdSwitches()
            local parts = {}
            local count = 0
            dz.devices().forEach(function(d)
                if (d.switchType ~= nil and d.switchType ~= '') then
                    count = count + 1
                    if (count <= 15) then
                        table.insert(parts, d.name .. '=' .. d.state)
                    end
                end
            end)
            if (count == 0) then return 'No switches found' end
            local msg = 'Switches: ' .. table.concat(parts, ' | ')
            if (count > 15) then
                msg = msg .. ' (+' .. (count - 15) .. ' more)'
            end
            return msg
        end

        local function cmdTemp()
            local parts = {}
            local count = 0
            dz.devices().forEach(function(d)
                if (d.temperature ~= nil) then
                    count = count + 1
                    if (count <= 10) then
                        local entry = d.name .. ' ' .. round(d.temperature) .. 'C'
                        if (d.humidity ~= nil) then
                            entry = entry .. '/' .. d.humidity .. '%'
                        end
                        table.insert(parts, entry)
                    end
                end
            end)
            if (count == 0) then return 'No temperature sensors found' end
            local msg = 'Temp: ' .. table.concat(parts, ' | ')
            if (count > 10) then
                msg = msg .. ' (+' .. (count - 10) .. ' more)'
            end
            return msg
        end

        local function cmdStatus()
            local msgs = {}
            local c = cmdClimate()
            if (c and not c:find('not found')) then table.insert(msgs, c) end
            local w = cmdWeather()
            if (w and not w:find('not found')) then table.insert(msgs, w) end
            local e = cmdEnergy()
            if (e and not e:find('not found')) then table.insert(msgs, e) end
            local h = cmdHome()
            if (h and not h:find('not found')) then table.insert(msgs, h) end
            if (#msgs == 0) then
                table.insert(msgs, 'No devices available')
            end
            return msgs
        end

        -- ?????????????????????????????????????????????????????????
        -- TIMER: drain queued replies, one per minute
        -- ?????????????????????????????????????????????????????????
        if (triggeredItem.isTimer) then
            local q = dz.data.pendingReplies
            if (#q > 0) then
                sendReply(table.remove(q, 1))
                dz.data.pendingReplies = q
            end
            return
        end

        -- ?????????????????????????????????????????????????????????
        -- DEVICE TRIGGER: Inbox changed — parse incoming message
        -- ?????????????????????????????????????????????????????????
        if (not triggeredItem.isDevice) then return end

        local raw = triggeredItem.text or triggeredItem.sValue or ''
        if (raw == '') then return end

        if (raw == dz.data.lastHandled) then return end
        dz.data.lastHandled = raw

        dz.log('Inbox received: ' .. raw, dz.LOG_INFO)

        -- Parse format: [ChannelName|sender] text  or  [P|sender] text
        local channel, sender, body = raw:match('^%[([^|]+)|([^%]]+)%]%s*(.*)$')
        if (channel == nil) then
            dz.log('Could not parse inbox message, ignoring.', dz.LOG_DEBUG)
            return
        end

        -- Only respond to messages on our channel
        if (channel ~= CHANNEL_NAME) then
            dz.log('Message on [' .. channel .. '], not our channel [' .. CHANNEL_NAME .. ']. Ignoring.', dz.LOG_DEBUG)
            return
        end

        body = trim(body)
        if (body == '') then return end

        -- Only respond to messages that start with the command prefix
        if (body:sub(1, #CMD_PREFIX) ~= CMD_PREFIX) then
            dz.log('No command prefix (' .. CMD_PREFIX .. '), ignoring.', dz.LOG_DEBUG)
            return
        end

        -- Strip the prefix
        body = trim(body:sub(#CMD_PREFIX + 1))
        if (body == '') then return end

        dz.log('Command from ' .. sender .. ': ' .. body, dz.LOG_INFO)

        -- ?????????????????????????????????????????????????????????
        -- Route command
        -- ?????????????????????????????????????????????????????????
        local cmd = lower(body)
        local reply = nil
        local replies = nil

        if (cmd == 'help' or cmd == '?') then
            reply = cmdHelp()
        elseif (cmd == 'status' or cmd == 'all') then
            replies = cmdStatus()
        elseif (cmd == 'climate' or cmd == 'klimaat' or cmd == 'temperature') then
            reply = cmdClimate()
        elseif (cmd == 'weather' or cmd == 'weer') then
            reply = cmdWeather()
        elseif (cmd == 'energy' or cmd == 'energie' or cmd == 'power') then
            reply = cmdEnergy()
        elseif (cmd == 'home' or cmd == 'huis') then
            reply = cmdHome()
        elseif (cmd == 'switches' or cmd == 'schakelaars') then
            reply = cmdSwitches()
        elseif (cmd == 'temp' or cmd == 'temps') then
            reply = cmdTemp()
        elseif (cmd:sub(1, 7) == 'device ' or cmd:sub(1, 9) == 'apparaat ') then
            local devName = trim(body:sub(cmd:find(' ') + 1))
            reply = cmdDevice(devName)
        else
            reply = 'Unknown command. Send "' .. CMD_PREFIX .. 'help" for options.'
        end

        -- ?????????????????????????????????????????????????????????
        -- Queue reply/replies
        -- ?????????????????????????????????????????????????????????
        if (replies ~= nil and #replies > 0) then
            sendReply(table.remove(replies, 1))
            local q = dz.data.pendingReplies
            for _, msg in ipairs(replies) do
                table.insert(q, msg)
            end
            dz.data.pendingReplies = q
            dz.log('Queued ' .. #replies .. ' additional reply message(s)', dz.LOG_INFO)
        elseif (reply ~= nil) then
            sendReply(reply)
        end
    end
}
