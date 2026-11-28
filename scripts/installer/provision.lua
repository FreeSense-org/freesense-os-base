-- Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
-- Inspect media data in the boot environment. Emit fields only in a private work directory.
local lfs = require("lfs")
local hash = require("hash")
local directory = assert(arg[0]:match("^(.*)/[^/]+$"), "absolute program path required")
local decode = dofile(directory .. "/vendor/decoder.lua")()
local json_null = {}

local function checked(condition)
	if not condition then error("invalid provisioning input", 0) end
	return condition
end

local function chars(value, pattern, low, high)
	return type(value) == "string" and #value >= low and #value <= high and value:match(pattern)
end

local function valid_key(key)
	if type(key) ~= "string" then return false end
	key = key:upper():gsub("[%s%-]", ""):gsub("^FLT", ""):gsub("O", "0"):gsub("[IL]", "1")
	local alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
	if not chars(key, "^[" .. alphabet .. "]+$", 20, 20) then return false end
	local index = 1 + math.floor(hash.sha256(key:sub(1, 19)):digest():byte(1) / 8)
	return key:sub(20) == alphabet:sub(index, index)
end

local function transport(link)
	checked(type(link) == "string")
	link = link:match("^%s*(.-)%s*$")
	local authority, fragment = link:match("^https://([^/#?%s]+)/fleet/join#(.+)$")
	checked(authority)
	local host, port = authority:match("^(.-):(%d+)$")
	host = host or authority
	if port then checked(#port <= 5 and tonumber(port) >= 1 and tonumber(port) <= 65535) end
	checked(host:match("^%[[%x:%.]+%]$") or (#host <= 253 and host:match("^[A-Za-z0-9][A-Za-z0-9.%-]*$") and host:match("[A-Za-z0-9]$")))
	local fields = {}
	for field in (fragment .. "."):gmatch("(.-)%.") do fields[#fields+1] = field end
	checked(chars(fields[1], "^[0-9a-f]+$", 12, 12) and chars(fields[2], "^[A-Za-z0-9_%-]+$", 43, 43)
		and chars(fields[3], "^[0-9a-f]+$", 64, 64))
	local method = ({t="tunnel", s="tailscale", d="direct"})[fields[4]]
	checked(method and (#fields == 4 or (#fields == 5 and fields[4] == "s" and chars(fields[5], "^[A-Za-z0-9_%-]+$", 60, 800))))
	return method
end

local function sealed_field(value, bytes)
	if type(value) ~= "string" or #value % 4 ~= 0 then return false end
	local body, padding = value:match("^([A-Za-z0-9+/]+)(=*)$")
	if not body or #padding > 2 then return false end
	local length = (#value // 4) * 3 - #padding
	return length > 0 and (not bytes or length == bytes)
end

local function sealed_key(value)
	return type(value) == "table" and sealed_field(value.salt, 16) and sealed_field(value.nonce, 12)
		and sealed_field(value.tag, 16) and sealed_field(value.data)
end

local function seed_fields(text)
	checked(#text <= 65536 and utf8.len(text))
	local seed = decode(text, nil, json_null)
	checked(type(seed) == "table" and math.type(seed.version) == "integer" and seed.version == 1)
	local mode = seed.mode
	checked(mode == "attended" or mode == "pin" or mode == "unattended")
	local method = transport(seed.link)
	if seed.expires ~= nil then checked(math.type(seed.expires) == "integer" and seed.expires >= os.time()) end
	if mode == "unattended" then checked(valid_key(seed.key)) else checked(seed.key == nil) end
	if mode == "pin" then checked(sealed_key(seed.sealed_key)) else checked(seed.sealed_key == nil) end
	local ts = seed.tailscale
	if ts ~= nil then
		checked(type(ts) == "table" and ((ts.key ~= nil) ~= (ts.sealed ~= nil)))
		if ts.key ~= nil then
			checked(mode == "unattended" and type(ts.key) == "string"
				and chars(ts.key:match("^tskey%-auth%-(.*)$"), "^[A-Za-z0-9%-]+$", 10, 200))
		else
			checked(sealed_key(ts.sealed))
		end
	end
	return mode, method
end

local function plan_fields(text)
	checked(utf8.len(text))
	text = text:gsub("\r\n", "\n"):gsub("\n$", "")
	local fields = {}
	local allowed = {version=true, action=true, machine_uuid=true, target_id=true, lan=true, fleet_seed=true, admin_password_hash=true}
	for line in (text .. "\n"):gmatch("(.-)\n") do
		local key, value = line:match("^([a-z_]+)=(.+)$")
		checked(key and allowed[key] and not fields[key] and not value:find("[%z\1-\31\127]"))
		fields[key] = value
	end
	checked(fields.version == "1" and fields.action == "install")
	local uuid = fields.machine_uuid
	checked(type(uuid) == "string" and uuid:match("^%x%x%x%x%x%x%x%x%-%x%x%x%x%-%x%x%x%x%-%x%x%x%x%-%x%x%x%x%x%x%x%x%x%x%x%x$"))
	fields.machine_uuid = uuid:lower()
	checked(uuid:gsub("%-", ""):match("[^0]") and uuid:lower():gsub("%-", ""):match("[^f]"))
	checked(chars(fields.target_id, "^[A-Za-z0-9][A-Za-z0-9%.:%- _]+$", 2, 128)
		and not fields.target_id:find("%s$") and not fields.target_id:upper():find("^GEOM%-"))
	if fields.lan then checked(chars(fields.lan, "^[A-Za-z0-9_.][A-Za-z0-9_.%-]*$", 1, 15)) end
	if fields.fleet_seed then fields.fleet_mode, fields.transport = seed_fields(fields.fleet_seed) end
	-- A bcrypt hash only (60 characters, cost 10 to 29). A plaintext password never validates.
	if fields.admin_password_hash then
		checked(#fields.admin_password_hash == 60 and fields.admin_password_hash:match("^%$2[aby]%$[12]%d%$[%./A-Za-z0-9]+$"))
	end
	fields.action, fields.version = nil, nil
	return fields
end

local function inspect()
	local mode, source, work = arg[1], arg[2], arg[3]
	checked(#arg == 3 and (mode == "plan" or mode == "seed"))
	checked(lfs.attributes(source, "mode") == "file" and lfs.attributes(work, "mode") == "directory")
	checked(lfs.attributes(work, "permissions") == "700" and lfs.attributes(work, "uid") == 0 and lfs.attributes(work, "gid") == 0)
	for entry in lfs.dir(work) do checked(entry == "." or entry == "..") end
	local limit = mode == "plan" and 69632 or 65536
	checked(lfs.attributes(source, "size") <= limit)
	local input = checked(io.open(source, "rb"))
	local text = input:read(limit+1) or ""
	checked(input:close() and #text <= limit)
	local fields
	if mode == "seed" then
		local kind, method = seed_fields(text)
		fields = {fleet_seed=text, fleet_mode=kind, transport=method}
	else
		fields = plan_fields(text)
	end
	for key, value in pairs(fields) do
		local output = checked(io.open(work .. "/" .. key, "wb"))
		checked(output:write(value) and output:close())
	end
end

-- Decoder and system errors can contain excerpts or filenames. Keep diagnostics fixed.
if not pcall(inspect) then
	io.stderr:write("Invalid, expired, or unreadable provisioning input.\n")
	os.exit(1)
end
