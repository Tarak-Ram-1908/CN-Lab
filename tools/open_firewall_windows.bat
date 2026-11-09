@echo off
REM Allow ClassBoard through the Windows firewall (run as Administrator on the SERVER laptop).
REM TCP 5050 = whiteboard sessions, UDP 5051 = "find servers on this network".
netsh advfirewall firewall add rule name="ClassBoard TCP 5050" dir=in action=allow protocol=TCP localport=5050 profile=private,domain
netsh advfirewall firewall add rule name="ClassBoard UDP 5051" dir=in action=allow protocol=UDP localport=5051 profile=private,domain
echo.
echo Done. Also check that the Wi-Fi / hotspot network is set to "Private" in Windows settings.
pause
