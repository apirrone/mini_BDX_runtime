import adafruit_bno055
import time
import serial
from scipy.spatial.transform import Rotation as R

uart = serial.Serial("/dev/ttyS0")  # , baudrate=115200)
imu = adafruit_bno055.BNO055_UART(uart)

while True:
    try:
        raw_orientation = imu.quaternion  # quat
        euler = R.from_quat(raw_orientation).as_euler("xyz")
        print(euler)
    except Exception as e:
        print(e)
        continue

    time.sleep(1 / 30)
