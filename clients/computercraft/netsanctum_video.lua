local CONFIG_PATH = ".netsanctum-video.cfg"
local FRAME_INTERVAL = 0.5
local SEEK_STEP = 10

local args = { ... }
if args[1] == "reset" then
  if fs.exists(CONFIG_PATH) then fs.delete(CONFIG_PATH) end
  print("NetSanctum configuration removed.")
  return
end

if not http then error("The CC:Tweaked HTTP API is disabled", 0) end

local function loadConfig()
  if not fs.exists(CONFIG_PATH) then return nil end
  local file = fs.open(CONFIG_PATH, "r")
  local value = textutils.unserialize(file.readAll())
  file.close()
  if type(value) ~= "table" or type(value.url) ~= "string" or type(value.token) ~= "string" then
    return nil
  end
  return value
end

local function saveConfig(value)
  local file = assert(fs.open(CONFIG_PATH, "w"))
  file.write(textutils.serialize(value))
  file.close()
end

local config = loadConfig()
if not config then
  write("NetSanctum node URL: ")
  local url = read():gsub("/+$", "")
  write("Owner token: ")
  local token = read("*")
  config = { url = url, token = token }
  saveConfig(config)
end

config.url = config.url:gsub("/+$", "")
local allowed, reason = http.checkURL(config.url .. "/auth/login")
if not allowed then error("HTTP URL blocked by CC:Tweaked: " .. tostring(reason), 0) end

local bearer

local function decodeResponse(handle, err, failure)
  local response = handle or failure
  if not response then return nil, err or "HTTP request failed", nil end
  local code = response.getResponseCode()
  local raw = response.readAll()
  response.close()
  local data
  if raw and raw ~= "" then
    local ok, decoded = pcall(textutils.unserializeJSON, raw)
    if ok then data = decoded end
  end
  if code < 200 or code >= 300 then
    local detail = data and data.detail or err or ("HTTP " .. code)
    return nil, tostring(detail), code
  end
  return data, nil, code
end

local function login()
  local handle, err, failure = http.post({
    url = config.url .. "/auth/login",
    body = textutils.serializeJSON({ token = config.token }),
    headers = { ["Content-Type"] = "application/json", ["Accept"] = "application/json" },
    timeout = 15,
  })
  local data, requestError = decodeResponse(handle, err, failure)
  if not data or type(data.access_token) ~= "string" then
    error("NetSanctum login failed: " .. tostring(requestError), 0)
  end
  bearer = data.access_token
end

local function apiGet(path, retried)
  if not bearer then login() end
  local handle, err, failure = http.get({
    url = config.url .. path,
    headers = { ["Authorization"] = "Bearer " .. bearer, ["Accept"] = "application/json" },
    timeout = 25,
  })
  local data, requestError, code = decodeResponse(handle, err, failure)
  if code == 401 and not retried then
    bearer = nil
    login()
    return apiGet(path, true)
  end
  if not data then return nil, requestError end
  return data
end

local function apiOpen(path, binary, retried)
  if not bearer then login() end
  local handle, err, failure = http.get({
    url = config.url .. path,
    headers = { ["Authorization"] = "Bearer " .. bearer },
    binary = binary,
    timeout = 25,
  })
  local response = handle or failure
  if not response then return nil, err or "HTTP request failed" end
  local code = response.getResponseCode()
  if code >= 200 and code < 300 then return response end
  local raw = response.readAll()
  response.close()
  if code == 401 and not retried then
    bearer = nil
    login()
    return apiOpen(path, binary, true)
  end
  local detail = err or ("HTTP " .. code)
  if raw and raw ~= "" then
    local ok, decoded = pcall(textutils.unserializeJSON, raw)
    if ok and decoded.detail then detail = decoded.detail end
  end
  return nil, tostring(detail)
end

local function clip(value, low, high)
  return math.max(low, math.min(high, value))
end

local function fit(text, width)
  text = tostring(text or "(untitled)"):gsub("[%c]", " ")
  if #text > width then return text:sub(1, math.max(1, width - 1)) .. ">" end
  return text .. string.rep(" ", math.max(0, width - #text))
end

local function center(text, width)
  if #text >= width then return text:sub(1, width) end
  local left = math.floor((width - #text) / 2)
  return string.rep(" ", left) .. text .. string.rep(" ", width - #text - left)
end

local function buttonBar(labels, width)
  local result = {}
  for index, label in ipairs(labels) do
    local first = math.floor((index - 1) * width / #labels)
    local last = math.floor(index * width / #labels)
    result[#result + 1] = center(label, last - first)
  end
  return table.concat(result)
end

local nativeTerm = term.current()
local monitor
local monitorName
if args[1] and args[1] ~= "terminal" then
  monitor = peripheral.wrap(args[1])
  if not monitor or peripheral.getType(args[1]) ~= "monitor" then
    error("No monitor peripheral named " .. args[1], 0)
  end
  monitorName = args[1]
elseif args[1] ~= "terminal" then
  local selectedMonitor
  monitor = peripheral.find("monitor", function(name)
    if selectedMonitor then return false end
    selectedMonitor = name
    return true
  end)
  monitorName = selectedMonitor
end
if monitor then
  pcall(monitor.setTextScale, 0.5)
end

local speaker = peripheral.find("speaker")

local function loadVideos()
  local videos, requestError = apiGet("/api/video-archiver/videos?status=completed")
  if not videos then error("Cannot load videos: " .. tostring(requestError), 0) end
  local playable = {}
  for _, video in ipairs(videos) do
    if video.file_path then playable[#playable + 1] = video end
  end
  return playable
end

local function chooseVideo(videos, selected)
  if #videos == 0 then
    term.clear()
    term.setCursorPos(1, 1)
    print("No completed videos with a local file.")
    print(monitor and "Touch to retry." or "Press R to retry or Q to quit.")
    while true do
      local event, value = os.pullEvent()
      if event == "key" and value == keys.r then return nil, "reload" end
      if event == "key" and value == keys.q then return nil, "quit" end
      if event == "monitor_touch" and value == monitorName then return nil, "reload" end
    end
  end

  selected = clip(selected or 1, 1, #videos)
  while true do
    local width, height = term.getSize()
    local visible = math.max(1, height - 3)
    local first = clip(selected - math.floor(visible / 2), 1, math.max(1, #videos - visible + 1))
    term.setBackgroundColor(colors.black)
    term.setTextColor(colors.white)
    term.clear()
    term.setCursorPos(1, 1)
    term.setTextColor(colors.cyan)
    local header = "NetSanctum Video"
    if monitor then header = header .. string.rep(" ", math.max(1, width - #header - 3)) .. "[X]" end
    term.write(fit(header, width))
    for line = 1, visible do
      local index = first + line - 1
      term.setCursorPos(1, line + 1)
      if index <= #videos then
        term.setTextColor(index == selected and colors.black or colors.lightGray)
        term.setBackgroundColor(index == selected and colors.lightBlue or colors.black)
        term.write(fit(index .. " " .. videos[index].title, width))
      end
    end
    term.setBackgroundColor(colors.black)
    term.setTextColor(colors.gray)
    term.setCursorPos(1, height)
    term.write(fit("UP/DOWN select  ENTER play  R reload  Q quit", width))

    local event, key, x, y = os.pullEvent()
    if event == "key" then
      if key == keys.up then selected = clip(selected - 1, 1, #videos) end
      if key == keys.down then selected = clip(selected + 1, 1, #videos) end
      if key == keys.enter then return selected, "play" end
      if key == keys.r then return selected, "reload" end
      if key == keys.q then return selected, "quit" end
    elseif event == "monitor_touch" and key == monitorName then
      if y == 1 and x > width - 4 then return selected, "quit" end
      local index = first + y - 2
      if index >= 1 and index <= #videos then return index, "play" end
    end
  end
end

local function playVideo(video)
  local position = 0
  local startedAt = os.epoch("utc") / 1000
  local playing = true
  local refresh = true
  local lastError
  local audioError
  local audioGeneration = 0
  local stopped = false
  local result = 0
  local duration = tonumber(video.duration) or 0

  local function currentPosition()
    if playing then return position + os.epoch("utc") / 1000 - startedAt end
    return position
  end

  local function resetAudio()
    audioGeneration = audioGeneration + 1
    if speaker then speaker.stop() end
  end

  local function seek(target)
    position = clip(target, 0, duration)
    startedAt = os.epoch("utc") / 1000
    refresh = true
    resetAudio()
  end

  local function togglePlayback()
    if playing then
      position = clip(currentPosition(), 0, duration)
      playing = false
    else
      if position >= duration then position = 0 end
      startedAt = os.epoch("utc") / 1000
      playing = true
    end
    resetAudio()
  end

  local function finish(value)
    result = value
    stopped = true
    resetAudio()
  end

  local function videoLoop()
    while not stopped do
      local width, height = term.getSize()
      local frameWidth = width
      local frameHeight = math.max(1, height - 2)
      local now = clip(currentPosition(), 0, duration)
      if playing and duration > 0 and now >= duration then
        position = duration
        playing = false
        refresh = true
        resetAudio()
      end

      if refresh then
        local path = "/api/video-archiver/videos/" .. textutils.urlEncode(tostring(video.id))
          .. "/frame?format=cc-palette&fit=contain&time=" .. string.format("%.3f", now)
          .. "&width=" .. frameWidth .. "&height=" .. frameHeight
        local frame, requestError = apiGet(path)
        if frame and type(frame.rows) == "table" then
          term.setBackgroundColor(colors.black)
          term.clear()
          for row = 1, math.min(frameHeight, #frame.rows) do
            local colorsRow = frame.rows[row]
            term.setCursorPos(1, row)
            term.blit(string.rep(" ", #colorsRow), string.rep("0", #colorsRow), colorsRow)
          end
          lastError = nil
        else
          lastError = requestError
        end
        refresh = false
      end

      term.setBackgroundColor(colors.black)
      term.setTextColor(colors.white)
      term.setCursorPos(1, math.max(1, height - 1))
      local state = playing and "PLAY" or "PAUSE"
      local sound = speaker and (audioError and " AUDIO!" or " AUDIO") or " SILENT"
      local status = string.format("%s%s %d/%ds %s", state, sound, math.floor(now), duration, video.title or "")
      term.write(fit(lastError and ("ERROR: " .. lastError) or status, width))
      term.setTextColor(colors.gray)
      term.setCursorPos(1, height)
      local help = monitor and buttonBar({ "BACK", "PREV", "-10", playing and "PAUSE" or "PLAY", "+10", "NEXT" }, width)
        or "SPACE pause  LEFT/RIGHT seek  N/P video  Q back"
      term.write(fit(help, width))

      local timer = playing and os.startTimer(FRAME_INTERVAL) or nil
      while not stopped do
        local event, value, x, y = os.pullEvent()
        if event == "timer" and value == timer then
          refresh = true
          break
        elseif event == "term_resize" or event == "monitor_resize" then
          refresh = true
          break
        elseif event == "key" then
          if value == keys.q or value == keys.backspace then finish(0) break end
          if value == keys.n then finish(1) break end
          if value == keys.p then finish(-1) break end
          if value == keys.space or value == keys.enter then togglePlayback() break end
          if value == keys.left then seek(currentPosition() - SEEK_STEP) break end
          if value == keys.right then seek(currentPosition() + SEEK_STEP) break end
          if value == keys.home then seek(0) break end
        elseif event == "monitor_touch" and value == monitorName and y == height then
          local zone = math.floor((x - 1) * 6 / math.max(1, width)) + 1
          if zone == 1 then finish(0) break end
          if zone == 2 then finish(-1) break end
          if zone == 3 then seek(currentPosition() - SEEK_STEP) break end
          if zone == 4 then togglePlayback() break end
          if zone == 5 then seek(currentPosition() + SEEK_STEP) break end
          if zone == 6 then finish(1) break end
        end
      end
    end
  end

  local function audioLoop()
    if not speaker then return end
    local dfpwm = require("cc.audio.dfpwm")
    while not stopped do
      if not playing or duration <= 0 then
        sleep(0.1)
      else
        local generation = audioGeneration
        local audioTime = clip(currentPosition(), 0, duration)
        local path = "/api/video-archiver/videos/" .. textutils.urlEncode(tostring(video.id))
          .. "/audio?format=dfpwm&time=" .. string.format("%.3f", audioTime)
        local handle, requestError = apiOpen(path, true)
        if not handle then
          audioError = requestError
          sleep(0.5)
        else
          audioError = nil
          local decoder = dfpwm.make_decoder()
          local receivedAudio = false
          while not stopped and playing and generation == audioGeneration do
            local chunk = handle.read(6 * 1024)
            if not chunk then break end
            receivedAudio = true
            local buffer = decoder(chunk)
            while not stopped and playing and generation == audioGeneration
              and not speaker.playAudio(buffer) do
              os.pullEvent()
            end
          end
          handle.close()
          if not receivedAudio then
            audioError = "No audio track"
            return
          end
          if not stopped and playing and generation == audioGeneration then sleep(0.25) end
        end
      end
    end
  end

  parallel.waitForAll(videoLoop, audioLoop)
  return result
end

local function run()
  login()
  local videos = loadVideos()
  if monitor then term.redirect(monitor) end
  local selected = 1
  while true do
    local action
    selected, action = chooseVideo(videos, selected)
    if action == "quit" then break end
    if action == "reload" then
      videos = loadVideos()
      selected = selected or 1
    elseif action == "play" then
      local move = playVideo(videos[selected])
      if move ~= 0 then
        selected = clip(selected + move, 1, #videos)
        while move ~= 0 do
          move = playVideo(videos[selected])
          if move ~= 0 then selected = clip(selected + move, 1, #videos) end
        end
      end
    end
  end
  term.setBackgroundColor(colors.black)
  term.setTextColor(colors.white)
  term.clear()
  term.setCursorPos(1, 1)
end

local ok, runError = xpcall(run, function(value) return tostring(value) end)
if speaker then speaker.stop() end
if monitor then term.redirect(nativeTerm) end
if not ok then error(runError, 0) end
