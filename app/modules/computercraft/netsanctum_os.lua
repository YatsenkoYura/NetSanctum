local CONFIG_PATH = ".netsanctum-os.cfg"
local LEGACY_CONFIG_PATH = ".netsanctum-video.cfg"
local FRAME_INTERVAL = 0.5
local SEEK_STEP = 10

local args = { ... }
if args[1] == "reset" then
  if fs.exists(CONFIG_PATH) then fs.delete(CONFIG_PATH) end
  if fs.exists(LEGACY_CONFIG_PATH) then fs.delete(LEGACY_CONFIG_PATH) end
  print("NetSanctumOS configuration removed.")
  return
end
if not http then error("The CC:Tweaked HTTP API is disabled", 0) end

local controller = term.current()

local function fit(text, width)
  text = tostring(text or ""):gsub("[%c]", " ")
  if #text > width then return text:sub(1, math.max(1, width - 1)) .. ">" end
  return text .. string.rep(" ", math.max(0, width - #text))
end

local function writeLine(target, y, text, foreground, background)
  local width = target.getSize()
  target.setBackgroundColor(background or colors.black)
  target.setTextColor(foreground or colors.white)
  target.setCursorPos(1, y)
  target.write(fit(text, width))
end

local function clear(target)
  target.setBackgroundColor(colors.black)
  target.setTextColor(colors.white)
  target.clear()
  target.setCursorPos(1, 1)
end

local function centered(target, y, text, foreground)
  local width = target.getSize()
  local left = math.max(1, math.floor((width - #text) / 2) + 1)
  target.setBackgroundColor(colors.black)
  target.setTextColor(foreground or colors.white)
  target.setCursorPos(left, y)
  target.write(text:sub(1, width))
end

local function peripheralNames(kind)
  local result = {}
  for _, name in ipairs(peripheral.getNames()) do
    if peripheral.getType(name) == kind then result[#result + 1] = name end
  end
  table.sort(result)
  return result
end

local monitorNames = peripheralNames("monitor")
local speakerNames = peripheralNames("speaker")
local monitorName
if args[1] and args[1] ~= "terminal" then
  if peripheral.getType(args[1]) ~= "monitor" then error("Monitor not found: " .. args[1], 0) end
  monitorName = args[1]
elseif args[1] ~= "terminal" then
  monitorName = monitorNames[1]
end
local display = monitorName and peripheral.wrap(monitorName) or controller
if monitorName then pcall(display.setTextScale, 0.5) end

local speakers = {}
for _, name in ipairs(speakerNames) do speakers[#speakers + 1] = peripheral.wrap(name) end

local function stopSpeakers()
  for _, speaker in ipairs(speakers) do speaker.stop() end
end

local function loadConfig()
  local path = fs.exists(CONFIG_PATH) and CONFIG_PATH
    or (fs.exists(LEGACY_CONFIG_PATH) and LEGACY_CONFIG_PATH or nil)
  if not path then return nil end
  local file = fs.open(path, "r")
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
  config = { url = url, token = read("*") }
  saveConfig(config)
elseif not fs.exists(CONFIG_PATH) then
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
    return nil, tostring(data and data.detail or err or ("HTTP " .. code)), code
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
    return apiGet(path, true)
  end
  return data, requestError
end

local function apiOpen(path, retried)
  if not bearer then login() end
  local handle, err, failure = http.get({
    url = config.url .. path,
    headers = { ["Authorization"] = "Bearer " .. bearer },
    binary = true,
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
    return apiOpen(path, true)
  end
  local ok, decoded = pcall(textutils.unserializeJSON, raw or "")
  return nil, tostring(ok and decoded.detail or err or ("HTTP " .. code))
end

local function bootLine(message, color)
  local _, height = controller.getSize()
  controller.scroll(1)
  writeLine(controller, height, "> " .. message, color or colors.lightGray)
  sleep(0.12)
end

local function boot()
  clear(controller)
  writeLine(controller, 1, "NETSANCTUM OS / BOOT", colors.cyan)
  if display ~= controller then
    clear(display)
    local _, height = display.getSize()
    centered(display, math.max(1, math.floor(height / 2) - 1), "NETSANCTUM", colors.cyan)
    centered(display, math.max(2, math.floor(height / 2) + 1), "INITIALIZING...", colors.lightGray)
  end
  local label = os.getComputerLabel() or ("Computer " .. os.getComputerID())
  bootLine("Controller: " .. label)
  bootLine("Monitors detected: " .. #monitorNames)
  bootLine(monitorName and ("Display: " .. monitorName) or "Display: internal terminal")
  bootLine("Speakers detected: " .. #speakers)
  bootLine("Authenticating node...")
  login()
  bootLine("Loading module registry...")
  local system, systemError = apiGet("/api/computercraft/system")
  if not system then error("NetSanctumOS initialization failed: " .. tostring(systemError), 0) end
  bootLine("Modules online: " .. #system.modules, colors.lime)
  sleep(0.3)
  return system
end

local function drawPreview(item, heading)
  if display == controller then return end
  clear(display)
  local width, height = display.getSize()
  writeLine(display, 1, heading or "NETSANCTUM", colors.cyan)
  centered(display, math.max(3, math.floor(height / 2) - 2), tostring(item.title or "Untitled"), colors.white)
  centered(display, math.max(4, math.floor(height / 2)), tostring(item.subtitle or item.kind or ""), colors.lightGray)
  writeLine(display, height, "Selected on controller", colors.gray)
end

local function selectList(title, items, preview)
  if #items == 0 then
    clear(controller)
    writeLine(controller, 1, title, colors.cyan)
    writeLine(controller, 3, "No items available.", colors.orange)
    writeLine(controller, 5, "Press BACKSPACE", colors.gray)
    repeat
      local _, key = os.pullEvent("key")
    until key == keys.backspace or key == keys.q
    return nil
  end
  local selected = 1
  while true do
    clear(controller)
    local width, height = controller.getSize()
    local visible = math.max(1, height - 3)
    local first = math.max(1, math.min(selected - math.floor(visible / 2), #items - visible + 1))
    writeLine(controller, 1, title, colors.cyan)
    for line = 1, visible do
      local index = first + line - 1
      if index <= #items then
        local item = items[index]
        local marker = index == selected and "> " or "  "
        writeLine(
          controller,
          line + 1,
          marker .. tostring(item.title or item.id),
          index == selected and colors.black or colors.lightGray,
          index == selected and colors.lightBlue or colors.black
        )
      end
    end
    writeLine(controller, height, "UP/DOWN  ENTER  BACK", colors.gray)
    if preview then preview(items[selected]) end

    local event, value, x, y = os.pullEvent()
    if event == "key" then
      if value == keys.up then selected = math.max(1, selected - 1) end
      if value == keys.down then selected = math.min(#items, selected + 1) end
      if value == keys.enter then return items[selected] end
      if value == keys.backspace or value == keys.q then return nil end
    elseif event == "mouse_scroll" then
      selected = math.max(1, math.min(#items, selected + value))
    elseif event == "mouse_click" and y >= 2 and y <= visible + 1 then
      local index = first + y - 2
      if index <= #items then
        if index == selected then return items[index] end
        selected = index
      end
    end
  end
end

local function queryValue(value)
  return value and textutils.urlEncode(tostring(value)) or nil
end

local function resourcePath(moduleId, itemId, suffix, childId, page)
  local path = "/api/computercraft/modules/" .. queryValue(moduleId)
    .. "/items/" .. queryValue(itemId) .. "/" .. suffix
  local query = {}
  if childId then query[#query + 1] = "child_id=" .. queryValue(childId) end
  if page ~= nil then query[#query + 1] = "page=" .. page end
  if #query > 0 then path = path .. "?" .. table.concat(query, "&") end
  return path
end

local function drawFrame(frame)
  clear(display)
  for row = 1, math.min(select(2, display.getSize()), #frame.rows) do
    local colorRow = frame.rows[row]
    display.setCursorPos(1, row)
    display.blit(string.rep(" ", #colorRow), string.rep("0", #colorRow), colorRow)
  end
end

local function wrapText(text, width)
  local lines = {}
  for paragraph in (tostring(text or "") .. "\n"):gmatch("(.-)\n") do
    local line = ""
    for originalWord in paragraph:gmatch("%S+") do
      local word = originalWord
      while #word > width do
        if line ~= "" then lines[#lines + 1] = line line = "" end
        lines[#lines + 1] = word:sub(1, width)
        word = word:sub(width + 1)
      end
      if line == "" then
        line = word
      elseif #line + #word + 1 <= width then
        line = line .. " " .. word
      else
        lines[#lines + 1] = line
        line = word
      end
    end
    if line ~= "" then lines[#lines + 1] = line end
    lines[#lines + 1] = ""
  end
  return lines
end

local function readText(moduleId, item, child)
  local data, requestError = apiGet(resourcePath(moduleId, item.id, "text", child.id))
  if not data then error("Reader failed: " .. tostring(requestError), 0) end
  local width, height = display.getSize()
  local bodyHeight = math.max(1, height - 2)
  local lines = wrapText(data.text, width)
  local offset = 1
  while true do
    clear(display)
    writeLine(display, 1, child.title, colors.cyan)
    for row = 1, bodyHeight do writeLine(display, row + 1, lines[offset + row - 1] or "") end
    writeLine(display, height, string.format("%d/%d", offset, math.max(1, #lines)), colors.gray)
    if display ~= controller then
      clear(controller)
      writeLine(controller, 1, "READER", colors.cyan)
      writeLine(controller, 3, child.title, colors.white)
      writeLine(controller, 5, "UP/DOWN scroll", colors.lightGray)
      writeLine(controller, 6, "PGUP/PGDN page", colors.lightGray)
      writeLine(controller, 8, "BACK return", colors.gray)
    end
    local event, key = os.pullEvent()
    if event == "key" then
      if key == keys.up then offset = math.max(1, offset - 1) end
      if key == keys.down then offset = math.min(math.max(1, #lines - bodyHeight + 1), offset + 1) end
      if key == keys.pageUp then offset = math.max(1, offset - bodyHeight) end
      if key == keys.pageDown then offset = math.min(math.max(1, #lines - bodyHeight + 1), offset + bodyHeight) end
      if key == keys.backspace or key == keys.q then return end
    elseif event == "mouse_scroll" then
      offset = math.max(1, math.min(math.max(1, #lines - bodyHeight + 1), offset + key * 3))
    end
  end
end

local function viewManga(moduleId, item, child)
  local page = 0
  local count = tonumber(child.pages_count) or 0
  while true do
    local width, height = display.getSize()
    local imageHeight = display == controller and math.max(1, height - 2) or height
    local framePath = resourcePath(moduleId, item.id, "frame", child.id, page)
      .. "&format=cc-palette&fit=contain&width=" .. width .. "&height=" .. imageHeight
    local frame, requestError = apiGet(framePath)
    if not frame then error("Page rendering failed: " .. tostring(requestError), 0) end
    drawFrame(frame)
    if display == controller then
      writeLine(controller, height - 1, string.format("PAGE %d/%d  LEFT/RIGHT", page + 1, count), colors.white)
      writeLine(controller, height, "BACK return", colors.gray)
    else
      clear(controller)
      writeLine(controller, 1, "MANGA VIEWER", colors.cyan)
      writeLine(controller, 3, item.title, colors.white)
      writeLine(controller, 5, string.format("Page %d / %d", page + 1, count), colors.lightGray)
      writeLine(controller, 7, "LEFT/RIGHT page", colors.lightGray)
      writeLine(controller, 9, "BACK return", colors.gray)
    end
    local _, key = os.pullEvent("key")
    if key == keys.left then page = math.max(0, page - 1) end
    if key == keys.right then page = math.min(math.max(0, count - 1), page + 1) end
    if key == keys.backspace or key == keys.q then return end
  end
end

local function playMedia(moduleId, item, child)
  local kind = child and child.kind or item.kind
  local title = child and child.title or item.title
  local childId = child and child.id or nil
  local duration = tonumber((child and child.duration) or item.duration) or 0
  if kind == "audio" and #speakers == 0 then
    clear(display)
    centered(display, 3, "NO SPEAKERS DETECTED", colors.orange)
    clear(controller)
    writeLine(controller, 1, "PLAYER UNAVAILABLE", colors.orange)
    writeLine(controller, 3, "Attach a speaker peripheral.", colors.lightGray)
    writeLine(controller, 5, "Press BACK", colors.gray)
    repeat
      local _, key = os.pullEvent("key")
    until key == keys.backspace or key == keys.q
    return
  end
  local position = 0
  local startedAt = os.epoch("utc") / 1000
  local playing = true
  local stopped = false
  local generation = 0
  local audioEnded = false

  local function currentPosition()
    if playing then return position + os.epoch("utc") / 1000 - startedAt end
    return position
  end

  local function resetAudio()
    generation = generation + 1
    audioEnded = false
    stopSpeakers()
  end

  local function seek(target)
    position = math.max(0, duration > 0 and math.min(duration, target) or target)
    startedAt = os.epoch("utc") / 1000
    resetAudio()
  end

  local function toggle()
    if playing then
      position = currentPosition()
      playing = false
    else
      if duration > 0 and position >= duration then position = 0 end
      startedAt = os.epoch("utc") / 1000
      playing = true
    end
    resetAudio()
  end

  local function uiLoop()
    local redraw = true
    while not stopped do
      local now = currentPosition()
      if duration > 0 and now >= duration then
        position = duration
        playing = false
        resetAudio()
      end
      if redraw then
        if kind == "video" or kind == "episode" then
          local width, height = display.getSize()
          if display == controller then height = math.max(1, height - 3) end
          local path = resourcePath(moduleId, item.id, "frame", childId)
            .. (childId and "&" or "?") .. "format=cc-palette&fit=contain&time="
            .. string.format("%.3f", now) .. "&width=" .. width .. "&height=" .. height
          local frame = apiGet(path)
          if frame then
            if duration == 0 and tonumber(frame.duration) then duration = tonumber(frame.duration) end
            drawFrame(frame)
          end
        else
          clear(display)
          local _, height = display.getSize()
          centered(display, math.max(2, math.floor(height / 2) - 1), "NOW PLAYING", colors.cyan)
          centered(display, math.max(3, math.floor(height / 2) + 1), title, colors.white)
          centered(display, math.max(4, math.floor(height / 2) + 3), item.subtitle or "", colors.lightGray)
        end
        redraw = false
      end
      if display == controller and (kind == "video" or kind == "episode") then
        local _, height = controller.getSize()
        writeLine(controller, height - 2, (playing and "PLAY " or "PAUSE ") .. title, colors.white)
        writeLine(controller, height - 1, "SPACE pause  LEFT/RIGHT seek", colors.lightGray)
        writeLine(controller, height, "BACK return", colors.gray)
      else
        clear(controller)
        writeLine(controller, 1, "PLAYER / " .. (playing and "PLAY" or (audioEnded and "ENDED" or "PAUSE")), colors.cyan)
        writeLine(controller, 3, title, colors.white)
        writeLine(controller, 5, string.format("Time: %ds%s", math.floor(now), duration > 0 and (" / " .. duration .. "s") or ""), colors.lightGray)
        writeLine(controller, 7, "SPACE play/pause", colors.lightGray)
        writeLine(controller, 8, "LEFT/RIGHT seek 10s", colors.lightGray)
        writeLine(controller, 10, "BACK return", colors.gray)
      end
      local timer = playing and os.startTimer(FRAME_INTERVAL) or nil
      while not stopped do
        local event, value = os.pullEvent()
        if event == "timer" and value == timer then redraw = true break end
        if event == "monitor_resize" or event == "term_resize" then redraw = true break end
        if event == "key" then
          if value == keys.space or value == keys.enter then toggle() redraw = true break end
          if value == keys.left then seek(currentPosition() - SEEK_STEP) redraw = true break end
          if value == keys.right then seek(currentPosition() + SEEK_STEP) redraw = true break end
          if value == keys.backspace or value == keys.q then stopped = true resetAudio() break end
        end
      end
    end
  end

  local function queueAudio(buffer, activeGeneration)
    local queued = {}
    while not stopped and playing and activeGeneration == generation do
      local pending = false
      for index, speaker in ipairs(speakers) do
        if not queued[index] then
          queued[index] = speaker.playAudio(buffer)
          if not queued[index] then pending = true end
        end
      end
      if not pending then return true end
      os.pullEvent()
    end
    return false
  end

  local function audioLoop()
    if #speakers == 0 then return end
    local dfpwm = require("cc.audio.dfpwm")
    while not stopped do
      if not playing or audioEnded then
        sleep(0.1)
      else
        local activeGeneration = generation
        local path = resourcePath(moduleId, item.id, "audio", childId)
          .. (childId and "&" or "?") .. "format=dfpwm&time=" .. string.format("%.3f", currentPosition())
        local handle = apiOpen(path)
        if not handle then
          audioEnded = true
        else
          local decoder = dfpwm.make_decoder()
          local received = false
          while not stopped and playing and activeGeneration == generation do
            local chunk = handle.read(6 * 1024)
            if not chunk then break end
            received = true
            if not queueAudio(decoder(chunk), activeGeneration) then break end
          end
          handle.close()
          if activeGeneration == generation and playing then
            audioEnded = true
            if kind == "audio" then
              position = currentPosition()
              playing = false
              stopSpeakers()
            elseif not received then
              stopSpeakers()
            end
          end
        end
      end
    end
  end

  parallel.waitForAll(uiLoop, audioLoop)
end

local function loadModuleItems(moduleId)
  local items = {}
  local offset = 0
  while offset do
    local path = "/api/computercraft/modules/" .. queryValue(moduleId)
      .. "/items?limit=200&offset=" .. offset
    local result, requestError = apiGet(path)
    if not result then error("Catalog failed: " .. tostring(requestError), 0) end
    for _, item in ipairs(result.items) do items[#items + 1] = item end
    offset = result.next_offset
  end
  return items
end

local function openLibrary(module)
  while true do
    local items = loadModuleItems(module.id)
    local item = selectList(module.title, items, function(selected) drawPreview(selected, module.title) end)
    if not item then return end
    if item.kind == "audio" or item.kind == "video" then
      playMedia(module.id, item)
    else
      local detail, detailError = apiGet(
        "/api/computercraft/modules/" .. queryValue(module.id) .. "/items/" .. queryValue(item.id)
      )
      if not detail then error("Details failed: " .. tostring(detailError), 0) end
      local child = selectList(item.title, detail.item.children or {}, function(selected)
        drawPreview(selected, item.title)
      end)
      if child then
        if item.kind == "novel" and child.readable then readText(module.id, item, child)
        elseif item.kind == "manga" and child.readable then viewManga(module.id, item, child)
        elseif child.playable then playMedia(module.id, item, child)
        end
      end
    end
  end
end

local function run()
  local system = boot()
  while true do
    local module = selectList("NETSANCTUM OS", system.modules, function(selected)
      drawPreview({ title = selected.title, subtitle = "MODULE ONLINE" }, "NETSANCTUM OS")
    end)
    if not module then break end
    openLibrary(module)
  end
end

local ok, runError = xpcall(run, function(value) return tostring(value) end)
stopSpeakers()
clear(controller)
if display ~= controller then clear(display) end
if not ok then error(runError, 0) end
