-- emerald_bridge.lua
-- Thin transport between mGBA and Python. Deliberately dumb: it moves bytes
-- and presses buttons, nothing else. All decoding lives in Python so that this
-- file, the only one that needs a manual reload in mGBA, stops changing.
--
-- Load in each mGBA window: Tools > Scripting > File > Load script.
-- The first window binds port 8888, the next 8889, and so on; the port it
-- bound is printed to the scripting console.
--
-- Protocol: one command per line, one line of reply.
--   ping                       -> pong
--   frame                      -> current frame number
--   read 0x020244EC 600        -> lowercase hex of those bytes (max 4096)
--   press A 8 8                -> hold A 8 frames, idle 8, reply ok when done
--   press UP,B                 -> defaults to 8 and 8
--   wait 30                    -> idle 30 frames, reply ok
--   screenshot <path>          -> ok
--   savestate <path>           -> ok | error
--   loadstate <path>           -> ok | error
--
-- There is deliberately no write command: the agents must never edit memory.

local BASE_PORT = 8888
local MAX_READ  = 4096

local BUTTONS = {
  A = C.GBA_KEY.A, B = C.GBA_KEY.B, START = C.GBA_KEY.START, SELECT = C.GBA_KEY.SELECT,
  UP = C.GBA_KEY.UP, DOWN = C.GBA_KEY.DOWN, LEFT = C.GBA_KEY.LEFT, RIGHT = C.GBA_KEY.RIGHT,
  L = C.GBA_KEY.L, R = C.GBA_KEY.R,
}

local queue = {}
local current = nil
local pendingReply = false
local client = nil
local buffer = ""

local function reply(text)
  if client then client:send(text .. "\n") end
end

callbacks:add("frame", function()
  if current == nil then
    if #queue == 0 then
      if pendingReply then
        pendingReply = false
        reply("ok")
      end
      return
    end
    current = table.remove(queue, 1)
    emu:setKeys(current.keys)
  end
  if current.hold > 0 then
    current.hold = current.hold - 1
    if current.hold == 0 then emu:setKeys(0) end
  elseif current.wait > 0 then
    current.wait = current.wait - 1
  end
  if current.hold == 0 and current.wait == 0 then current = nil end
end)

local function handle(line)
  local words = {}
  for w in line:gmatch("%S+") do words[#words + 1] = w end
  local cmd = (words[1] or ""):lower()

  if cmd == "ping" then
    reply("pong")

  elseif cmd == "frame" then
    reply(tostring(emu:currentFrame()))

  elseif cmd == "read" then
    local addr = tonumber(words[2])
    local len = tonumber(words[3])
    if addr == nil or len == nil or len < 1 or len > MAX_READ then
      reply("error bad read arguments")
      return
    end
    local data = emu:readRange(addr, len)
    reply((data:gsub(".", function(c) return string.format("%02x", c:byte()) end)))

  elseif cmd == "press" then
    local keys = 0
    for name in (words[2] or ""):gmatch("[^,]+") do
      local key = BUTTONS[name:upper()]
      if key == nil then
        reply("error unknown button " .. name)
        return
      end
      keys = keys | (1 << key)
    end
    queue[#queue + 1] = {keys = keys, hold = tonumber(words[3]) or 8, wait = tonumber(words[4]) or 8}
    pendingReply = true

  elseif cmd == "wait" then
    queue[#queue + 1] = {keys = 0, hold = 0, wait = tonumber(words[2]) or 30}
    pendingReply = true

  elseif cmd == "screenshot" then
    emu:screenshot(line:sub(12))
    reply("ok")

  elseif cmd == "savestate" then
    reply(emu:saveStateFile(line:sub(11)) and "ok" or "error")

  elseif cmd == "loadstate" then
    reply(emu:loadStateFile(line:sub(11)) and "ok" or "error")

  else
    reply("error unknown command")
  end
end

local function onClientData()
  while true do
    local data, err = client:receive(4096)
    if data == nil then
      if err ~= socket.ERRORS.AGAIN then
        console:log("agent disconnected")
        client:close()
        client = nil
      end
      return
    end
    buffer = buffer .. data
    while true do
      local line, rest = buffer:match("^(.-)\r?\n(.*)$")
      if line == nil then break end
      buffer = rest
      handle(line)
    end
  end
end

local function onServerData()
  local sock, err = server:accept()
  if sock == nil then
    console:error("accept failed: " .. tostring(err))
    return
  end
  if client then client:close() end
  client = sock
  buffer = ""
  client:add("received", onClientData)
  client:add("error", function() client = nil end)
  console:log("agent connected")
end

server = nil
local port = BASE_PORT
while server == nil and port < BASE_PORT + 8 do
  local sock, bindErr = socket.bind(nil, port)
  if sock and not bindErr then
    local _, listenErr = sock:listen()
    if listenErr then
      sock:close()
      port = port + 1
    else
      server = sock
    end
  else
    port = port + 1
  end
end

if server then
  server:add("received", onServerData)
  console:log("emerald bridge listening on port " .. port)
else
  console:error("could not bind a port")
end
