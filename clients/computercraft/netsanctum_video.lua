local target = "netsanctum"
local source = "https://raw.githubusercontent.com/YatsenkoYura/NetSanctum/main/app/modules/computercraft/netsanctum_os.lua"

print("netsanctum_video has moved to NetSanctumOS.")
if not fs.exists(target) then
  if not shell.run("wget", source, target) then error("Could not install NetSanctumOS", 0) end
end
shell.run(target, ...)
