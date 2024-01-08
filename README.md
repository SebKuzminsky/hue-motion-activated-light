Uses aiohue to listen to a motion sensor and control a light.

Create a Hue Bridge API key by following the instructions here:
<https://developers.meethue.com/develop/get-started-2/>

```
$ sudo apt install python3-aiohttp python3-awesomeversion
$ pip install --break-system-packages --user aiohue
$ ./motion-light.py ${HUE_BRIDGE_IP} $(cat api-key)
```
