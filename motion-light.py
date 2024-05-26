#!/usr/bin/env python3

import argparse
import astral
import asyncio
import datetime
import logging
import pathlib
import pytz
import subprocess
import sys

import aiohue
import aiohue.v2.models.device
import aiohue.v2.models.device_power
import aiohue.v2.models.light_level
import aiohue.v2.models.motion
import aiohue.v2.models.resource
import aiohue.v2.models.temperature


def find_device_owning_resource(resource, devices):
    for device in devices:
        if device.id == resource.owner.rid:
            return device
    return None


parser = argparse.ArgumentParser(description="AIOHue Example")
parser.add_argument("host", help="hostname of Hue bridge")
parser.add_argument("appkey", help="appkey for Hue bridge (filename or raw string)")
parser.add_argument("--debug", help="enable debug logging", action="store_true")
args = parser.parse_args()

appkey_file = pathlib.Path(args.appkey)
if appkey_file.is_file():
    with open(str(appkey_file), 'r') as f:
        args.appkey = f.readline().strip()

motion_sensor_device_name = 'Front Door outdoor motion sensor'

light_device_names = [ "Front porch light", 'Front walkway' ]

motion_detected = False
light_level = 0

# Handle to a coroutine that will run when motion has been absent for
# too long.
motion_timeout_handle = None

# Seconds after the last detected "end-of-motion" event to consider the
# motion over.
motion_timeout_delay = 5 * 60

tz = pytz.timezone("US/Mountain")

my_astral = astral.Astral()
my_location = my_astral['Denver']

light_off_state = {
    # Light's completely off.
    'on': False,
    'color_xy': None,
    'brightness': None,
    'light_temp': None
}

night_light_state = {
    # Dim light, reddish-yellow color, warm color temp.
    'on': True,
    'color_xy': [ 0.5, 0.45 ],
    'brightness': 75,
    'light_temp': 500
}

bright_light_state = {
    # Bright light, neutral color, cold color temp.
    # This is used for motion at night.
    'on': True,
    'color_xy': [ 0.35, 0.4 ],
    'brightness': 100,
    'light_temp': 153
}

daytime_motion_light_state = {
    # This is used for motion during the day.
    'on': False,
    'color_xy': [ 0.7, 0.25 ],
    'brightness': 50,
    'light_temp': 153
}

default_light_state = light_off_state


def notify(msg: str):
    print(f"notifying: {msg}")

    cmd = ['/home/seb/send-text', msg]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True
        )

    except subprocess.CalledProcessError as e:
        print(f"failed to run program:")
        print(f"    cmd: {cmd}")
        print(f"    returncode: {e.returncode}")
        print(f"    stdout: {e.stdout}")
        print(f"    stderr: {e.stderr}")
        raise e

    return result


def make_timedelta_str(d: datetime.timedelta):
    return make_seconds_str(d.total_seconds())


def make_seconds_str(seconds: float):
    timedelta_str = ""

    seconds_per_minute = 60
    seconds_per_hour = 60 * seconds_per_minute
    seconds_per_day = 24 * seconds_per_hour

    if seconds > seconds_per_day:
        days = int(seconds / seconds_per_day)
        timedelta_str += f"{days} days "
        seconds -= days * seconds_per_day

    hours = int(seconds / seconds_per_hour)
    seconds -= hours * seconds_per_hour

    minutes = int(seconds / seconds_per_minute)
    seconds -= minutes * seconds_per_minute

    seconds = int(seconds)

    timedelta_str += f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    return timedelta_str


def get_sun_up(tomorrow=False):
    global my_location
    global tz

    degrees_above_horizon = 4
    if tomorrow:
        tomorrow = datetime.datetime.now(tz) + datetime.timedelta(days=1)
        sun_up = my_location.time_at_elevation(degrees_above_horizon, astral.SUN_RISING, date=tomorrow)
    else:
        sun_up = my_location.time_at_elevation(degrees_above_horizon, astral.SUN_RISING)
    return sun_up


def get_sun_down():
    global my_location
    degrees_above_horizon = 4
    sun_down = my_location.time_at_elevation(degrees_above_horizon, astral.SUN_SETTING)
    return sun_down


def is_daytime():
    global tz
    if get_sun_up() < datetime.datetime.now(tz) < get_sun_down():
        return True
    return False


async def main():
    """Run Main execution."""
    if args.debug:
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)-15s %(levelname)-5s %(name)s -- %(message)s",
        )

    async with aiohue.HueBridgeV2(args.host, args.appkey) as bridge:
        global motion_detected
        global light_level

        bridge_lock = asyncio.Lock()

        print("Motion sensors:")
        for device in bridge.devices:
            if aiohue.v2.models.resource.ResourceTypes.MOTION in [x.rtype for x in device.services]:
                print(f"    {device.metadata.name} ({device.id})")

        print("Lights:")
        for device in bridge.devices:
            if aiohue.v2.models.resource.ResourceTypes.LIGHT in [x.rtype for x in device.services]:
                print(f"    {device.metadata.name} ({device.id})")

        print()

        # Find the devices:
        #     Outdoor Motion Sensor
        #     Light
        motion_device = None
        light_devices = []
        for device in bridge.devices:
            if device.metadata.name == motion_sensor_device_name:
                motion_device = device
                print(f"found motion device '{motion_device.metadata.name}' ({motion_device.id})")
                for service in motion_device.services:
                    print(f"    {service.rtype} ({service.rid})")

            elif device.metadata.name in light_device_names:
                light_devices.append(device)
                print(f"found light device '{device.metadata.name}' ({device.id})")
                for service in device.services:
                    print(f"    {service.rtype} ({service.rid})")

            else:
                #print()
                #print(f"device: {device}")
                pass

        if motion_device is None:
            raise SystemExit("motion sensor device not found")

        if len(light_devices) != len(light_device_names):
            raise SystemExit("light device(s) missing")

        # Find the sensors on the Outdoor Motion Sensor device.
        motion_sensor = None
        device_power_sensor = None
        light_level_sensor = None
        temperature_sensor = None
        for sensor in bridge.sensors:
            if type(sensor) is aiohue.v2.models.motion.Motion and sensor.owner.rid == motion_device.id:
                motion_sensor = sensor
                motion_detected = motion_sensor.motion.motion
                print(f"found motion sensor: valid={motion_sensor.motion.motion_valid}, motion={motion_sensor.motion.motion}")
            elif type(sensor) is aiohue.v2.models.temperature.Temperature and sensor.owner.rid == motion_device.id:
                temperature_sensor = sensor
                print(f"found temperature sensor: valid={temperature_sensor.temperature.temperature_valid}, temperature={temperature_sensor.temperature.temperature} °C")
            elif type(sensor) is aiohue.v2.models.light_level.LightLevel and sensor.owner.rid == motion_device.id:
                light_level_sensor = sensor
                light_level = light_level_sensor.light.light_level
                print(f"found light_level sensor: valid={light_level_sensor.light.light_level_valid} light_level={light_level_sensor.light.light_level}")
            elif type(sensor) is aiohue.v2.models.device_power.DevicePower and sensor.owner.rid == motion_device.id:
                device_power_sensor = sensor
                print(f"found device_power sensor: battery_level={device_power_sensor.power_state.battery_level}%")
        if None in [motion_sensor, device_power_sensor, light_level_sensor, temperature_sensor]:
            raise SystemExit("expected sensor not found")

        # Find the light resources corresponding to the selected light devices.
        lights = []
        for light in bridge.lights:
            if light.owner.rid in [ x.id for x in light_devices]:
                lights.append(light)
                #print(f"found light: on={light.on.on}")
        if len(lights) != len(light_devices):
            raise SystemExit("light(s) missing")


#        async def motion_detected_flash(porch_light):
#            print("motion detected!")
#
#            async def flash():
#                red = color.rgb_to_xy(255, 0, 0)
#                blue = color.rgb_to_xy(0, 0, 255)
#                delay = 0.25
#                await bridge.lights.set_state(id = porch_light.id, color_xy=blue, transition_time=0)
#                await asyncio.sleep(delay)
#                await bridge.lights.set_state(id = porch_light.id, color_xy=red, transition_time=0)
#                await asyncio.sleep(delay)
#
#            await bridge.lights.set_state(
#                id = porch_light.id,
#                on = True,
#                brightness = 100,
#                color_temp = 500,   # 153-500
#                transition_time = 0
#            )
#
#            await flash()
#            await flash()
#            await flash()
#            await flash()
#            await flash()
#            await flash()
#
#            print("motion done!")


        async def handle_state():
            #
            # Process the current state, as updated by the event we just parsed.
            #

            global motion_detected
            global motion_timeout_handle
            global light_level
            global default_light_state
            global tz

            print(f"handling state: light_level={light_level}, motion_detected={motion_detected}, motion_timeout_handle={motion_timeout_handle}")

            if motion_detected and not motion_timeout_handle:
                #notify("new motion on the front porch!")
                pass

#            if motion_detected and not old_motion_detected and light_level < dusk_light_level:
#                print("new commotion on the porch!")
#                await motion_detected_flash(porch_light)

            if motion_detected or motion_timeout_handle:
                # There's motion on the porch, currently or recently...

                if is_daytime():
                    # ... but it's bright out, no need for more light.
                    porch_light_state = daytime_motion_light_state
                    print("motion on the porch, but daytime")

                else:
                    # ... and it's dark, let's turn on the porch light.
                    porch_light_state = bright_light_state
                    print("motion on the porch, in the dark: light on bright")

            else:
                # No motion, default light state is set by the Sun handler.
                porch_light_state = default_light_state
                print("no motion on the porch, reverting to sun-controlled light state")

            print(f"setting front-yard lights: on={porch_light_state['on']}, brightness={porch_light_state['brightness']}, color_xy={porch_light_state['color_xy']}, color_temp={porch_light_state['light_temp']}")

            for light in lights:
                light_device = find_device_owning_resource(light, light_devices)
                if light_device is None:
                    print(f"    light device for light resource {light.id} not found!")
                    next
                print(f"    {light_device.metadata.name}")
                try:
                    await bridge.lights.set_state(
                        id = light.id,
                        on = porch_light_state['on'],
                        brightness = porch_light_state['brightness'],
                        color_xy = porch_light_state['color_xy'],
                        color_temp = porch_light_state['light_temp'],
                        transition_time = 1000
                    )
                except aiohue.errors.AiohueException as e:
                    print(f"failed to control light '{light_device.metadata.name}':")
                    print(e)
                    print("is power to the light off?")


        async def handle_motion_timeout():
            global motion_timeout_handle

            await asyncio.sleep(motion_timeout_delay)
            now = datetime.datetime.now()
            print()
            print(datetime.datetime.isoformat(now, ' ', 'seconds'))
            print(f"motion timeout: it's been {make_seconds_str(motion_timeout_delay)} since the end of motion")
            motion_timeout_handle = None
            await handle_state()


        async def handle_event(event_type, item):
            global motion_timeout_handle
            global motion_detected
            global light_level

            async with bridge_lock:
                if event_type is not aiohue.v2.EventType.RESOURCE_UPDATED:
                    # unhandled event type
                    #print(f"{event_type}: {item}")
                    return

                if type(item) is aiohue.v2.models.motion.Motion and item.id == motion_sensor.id:
                    msg = f"motion: valid={item.motion.motion_valid}, motion={item.motion.motion}"
                    motion_detected = item.motion.motion_valid and item.motion.motion
                    if not motion_detected:
                        if motion_timeout_handle is not None:
                            motion_timeout_handle.cancel()
                            motion_timeout_handle = None
                        motion_timeout_handle = asyncio.create_task(handle_motion_timeout())

#                elif type(item) is aiohue.v2.models.temperature.Temperature and item.id == temperature_sensor.id:
#                    print(f"temperature: valid={item.temperature.temperature_valid}, temperature={item.temperature.temperature} °C")

                elif type(item) is aiohue.v2.models.light_level.LightLevel and item.id == light_level_sensor.id:
#                    print(f"light level: valid={item.light.light_level_valid} light_level={item.light.light_level}")
                    light_level = item.light.light_level
                    return

                elif type(item) is aiohue.v2.models.device_power.DevicePower and item.id == device_power_sensor.id:
                    msg = f"device power: battery_level={item.power_state.battery_level}%"
                    notify(f"front porch motion sensor battery level is {item.power_state.battery_level}%")

                else:
                    # unhandled Resource Update event
                    #if type(item) is aiohue.v2.models.light.Light and item.id == porch_light.id:
                    #    print(f"light change:")
                    #    print(f"    color_temp={item.color_temperature.mirek}")
                    #    print(f"    color={item.color.xy}")
                    #    print(f"    dimming={item.dimming}")
                    return

                now = datetime.datetime.now()
                print()
                print(datetime.datetime.isoformat(now, ' ', 'seconds'))
                print(msg)

                await handle_state()


        async def handle_sun():
            global default_light_state
            global light_off_state
            global night_light_state
            global tz

            while True:
                now = datetime.datetime.now(tz)
                sun_up = get_sun_up()
                sun_down = get_sun_down()
                print()
                print(f"thinking about the sun")
                print(f"    current time is {now.isoformat(' ', 'seconds')}")
                print(f"    sun-up is at {sun_up.isoformat(' ', 'seconds')}")
                print(f"    sun-down is at {sun_down.isoformat(' ', 'seconds')}")

                if now < sun_up:
                    print("    it's before sun-up, turning on night light")
                    default_light_state = night_light_state
                    sleep_duration = sun_up - now
                    print(f"    sleeping until sun-up ({sun_up.isoformat(' ', 'seconds')}, in {make_timedelta_str(sleep_duration)})")

                elif now < sun_down:
                    print("    it's between sun-up and sun-down, turning off light")
                    default_light_state = light_off_state
                    sleep_duration = sun_down - now
                    print(f"    sleeping until sun-down ({sun_down.isoformat(' ', 'seconds')}, in {make_timedelta_str(sleep_duration)})")

                else:
                    print("    it's after sun-down, turning on night light")
                    default_light_state = night_light_state
                    sun_up = get_sun_up(tomorrow=True)
                    sleep_duration = sun_up - now
                    print(f"    sleeping until sun-up tomorrow ({sun_up.isoformat(' ', 'seconds')}, in {make_timedelta_str(sleep_duration)})")

                await handle_state()
                await asyncio.sleep(sleep_duration.total_seconds())


        asyncio.create_task(handle_sun())
        bridge.subscribe(handle_event)
        while True:
            await asyncio.sleep(3600)


try:
    asyncio.run(main())
except KeyboardInterrupt:
    pass
