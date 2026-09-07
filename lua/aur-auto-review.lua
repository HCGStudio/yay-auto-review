-- Native yay >= 13.0.0 integration. Load from ~/.config/yay/init.lua:
--   require("aur-auto-review")
--
-- AURPreInstall runs after dependency resolution and Git checkout, before
-- clean/diff/edit menus and the first makepkg --verifysource invocation.
-- The makepkg guard checks the approved files again after those menus.

if type(yay) ~= "table"
    or type(yay.create_autocmd) ~= "function"
    or type(yay.abort) ~= "function"
    or type(yay.opt) ~= "table" then
    error("aur-auto-review requires yay >= 13.0.0 with native Lua hooks")
end

local command = "aur-auto-review"
local guard = "aur-auto-review-makepkg"

local function abort(message)
    yay.abort("aur-auto-review: " .. message)
    -- A real yay.abort raises a controlled error. Fail closed even if a
    -- caller replaces that function with one that unexpectedly returns.
    error("aur-auto-review: " .. message)
end

local function checked_string(value, name)
    if type(value) ~= "string" or value == "" or value:find("\0", 1, true) then
        abort("invalid " .. name .. " in AURPreInstall event")
    end
    return value
end

local function shell_quote(value)
    -- Every event field is untrusted package metadata. POSIX single quotes
    -- preserve whitespace, newlines, $, backticks and shell operators.
    return "'" .. value:gsub("'", "'\\''") .. "'"
end

local function succeeded(result, reason, code)
    -- gopher-lua / Lua 5.1 returns 0 on success. Lua 5.2+ returns
    -- true, "exit", 0. Never treat nonzero numbers as truthy success.
    if type(result) == "number" then
        return result == 0
    end
    return result == true and reason == "exit" and code == 0
end

-- Lua is loaded before yay parses CLI overrides and before --save writes
-- config.json. Inspect the actual process here, so transient build paths and
-- the guard cannot be persisted or disabled even on non-install commands.
local process_args = io.open("/proc/self/cmdline", "rb")
if not process_args then
    abort("cannot inspect yay command-line overrides")
end
local argv = process_args:read("*a")
process_args:close()
if type(argv) ~= "string" or argv == "" then
    abort("cannot read yay command-line overrides")
end
local first = true
for arg in argv:gmatch("[^%z]+") do
    if first then
        first = false
    elseif arg == "--" then
        break
    else
        local option = arg:match("^([^=]+)")
        if option == "--makepkg" or option == "--builddir"
            or option == "--mflags" or option == "--save" then
            abort("unsupported override while review is enabled: " .. option)
        end
    end
end

-- yay embeds gopher-lua, which provides os.setenv in addition to Lua 5.1's
-- standard library. The session is inherited by all hook and makepkg calls.
if type(os.setenv) ~= "function" then
    abort("this yay Lua runtime does not provide os.setenv")
end
local cache_home = os.getenv("XDG_CACHE_HOME")
if not cache_home or cache_home == "" then
    cache_home = checked_string(os.getenv("HOME"), "HOME") .. "/.cache"
end
cache_home = checked_string(cache_home, "cache directory")
if cache_home:sub(1, 1) ~= "/" then
    abort("XDG_CACHE_HOME must be an absolute path")
end

-- The helper creates the private build directory before yay tries to clone
-- into it. No package code is read or executed during session creation.
local pipe = io.popen(shell_quote(command) .. " 'session'", "r")
if not pipe then
    abort("cannot create a review session; check aur-auto-review is on PATH")
end
local output = pipe:read("*a")
local closed, close_reason, close_code = pipe:close()
-- gopher-lua returns an exit number for process.close(); PUC Lua 5.1
-- returns true, and newer Lua versions return true, "exit", 0.
if not succeeded(closed, close_reason, close_code)
    and not (closed == true and close_reason == nil and close_code == nil) then
    abort("review session helper failed")
end
local session = type(output) == "string" and output:match("^([0-9a-f]+)\n?$")
if not session or #session ~= 32 then
    abort("review session helper returned an invalid session")
end
if not os.setenv("AUR_AUTO_REVIEW_SESSION", session) then
    abort("cannot set review session environment")
end
local build_dir = cache_home:gsub("/+$", "") .. "/aur-auto-review/builds/" .. session

-- Fresh checkouts prevent stale, unreviewed build products from being reused.
-- Review results remain in a separate persistent cache across transactions.
-- Refresh even when .SRCINFO's version is unchanged: a maintainer can change
-- a recipe without bumping its version. The reviewer checks remote Git too.
yay.opt.build_dir = build_dir
yay.opt.redownload = "all"
yay.opt.makepkg_bin = guard

yay.create_autocmd("AURPreInstall", {
    desc = "Review AUR package files with Codex before confirmation and build",
    callback = function(event)
        -- Detect another Lua configuration entry overwriting the guard.
        -- yay.opt is not a live view of CLI overrides; the Python hook also
        -- validates the running yay command line before issuing approval.
        if yay.opt.makepkg_bin ~= guard or yay.opt.redownload ~= "all"
            or yay.opt.build_dir ~= build_dir
            or os.getenv("AUR_AUTO_REVIEW_SESSION") ~= session then
            abort("the review session, build_dir, makepkg guard or redownload setting was overwritten")
        end
        if type(event) ~= "table" or type(event.data) ~= "table" then
            abort("missing AURPreInstall event data")
        end

        local data = event.data
        local base = checked_string(data.base, "package base")
        local directory = checked_string(data.dir, "package directory")
        local version = checked_string(data.version, "version")
        if event.match ~= base or directory:sub(1, 1) ~= "/" then
            abort("inconsistent package base or non-absolute package directory")
        end

        local modified = data.last_modified
        if type(modified) ~= "number" or modified < 0
            or modified ~= modified or modified == math.huge
            or modified % 1 ~= 0 then
            abort("invalid last_modified in AURPreInstall event")
        end

        local args = {
            command, "hook", "--pkgbase", base, "--directory", directory,
            "--last-modified", string.format("%.0f", modified), "--version", version,
        }
        for index, value in ipairs(args) do
            args[index] = shell_quote(value)
        end

        -- os.execute inherits the terminal, so reports and the explicit
        -- confirmation remain visible. Missing executable, cancellation,
        -- refused review, invalid output and red findings all stop yay.
        local result, reason, code = os.execute(table.concat(args, " "))
        if not succeeded(result, reason, code) then
            abort("review failed or installation was not approved")
        end
    end,
})
