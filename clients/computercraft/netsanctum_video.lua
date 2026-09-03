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

local function clip(value, low, high)
  return math.max(low, math.min(high, value))
end

local function fit(text, width)
  text = tostring(text or "(untitled)"):gsub("[%c]", " ")
  if #text > width then return text:sub(1, math.max(1, width - 1)) .. ">" end
  return text .. string.rep(" ", math.max(0, width - #text))
end

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
    print("Press R to retry or Q to quit.")
    while true do
      local _, key = os.pullEvent("key")
      if key == keys.r then return nil, "reload" end
      if key == keys.q then return nil, "quit" end
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
    term.write(fit("NetSanctum Video", width))
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

    local event, key = os.pullEvent()
    if event == "key" then
      if key == keys.up then selected = clip(selected - 1, 1, #videos) end
      if key == keys.down then selected = clip(selected + 1, 1, #videos) end
      if key == keys.enter then return selected, "play" end
      if key == keys.r then return selected, "reload" end
      if key == keys.q then return selected, "quit" end
    end
  end
end

local function playVideo(video)
  local position = 0
  local startedAt = os.epoch("utc") / 1000
  local playing = true
  local refresh = true
  local lastError

  local function currentPosition()
    if playing then return position + os.epoch("utc") / 1000 - startedAt end
    return position
  end

  while true do
    local width, height = term.getSize()
    local frameWidth = width
    local frameHeight = math.max(1, height - 2)
    local duration = tonumber(video.duration) or 0
    local now = clip(currentPosition(), 0, duration)
    if duration > 0 and now >= duration then
      position = duration
      playing = false
      refresh = true
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
    local status = string.format("%s %d/%ds %s", state, math.floor(now), duration, video.title or "")
    term.write(fit(lastError and ("ERROR: " .. lastError) or status, width))
    term.setTextColor(colors.gray)
    term.setCursorPos(1, height)
    term.write(fit("SPACE pause  LEFT/RIGHT seek  N/P video  Q back", width))

    local timer = playing and os.startTimer(FRAME_INTERVAL) or nil
    while true do
      local event, value = os.pullEvent()
      if event == "timer" and value == timer then
        refresh = true
        break
      elseif event == "term_resize" or event == "monitor_resize" then
        refresh = true
        break
      elseif event == "key" then
        if value == keys.q or value == keys.backspace then return 0 end
        if value == keys.n then return 1 end
        if value == keys.p then return -1 end
        if value == keys.space or value == keys.enter then
          if playing then
            position = clip(currentPosition(), 0, duration)
            playing = false
          else
            startedAt = os.epoch("utc") / 1000
            playing = true
          end
          break
        end
        if value == keys.left or value == keys.right or value == keys.home then
          local target = value == keys.home and 0 or currentPosition()
          if value == keys.left then target = target - SEEK_STEP end
          if value == keys.right then target = target + SEEK_STEP end
          position = clip(target, 0, duration)
          startedAt = os.epoch("utc") / 1000
          refresh = true
          break
        end
      end
    end
  end
end

login()
local videos = loadVideos()
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
