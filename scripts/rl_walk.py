import pickle
import time
from queue import Queue
from threading import Thread

import adafruit_bno055
import FramesViewer.utils as fv_utils
import numpy as np
import serial
from scipy.spatial.transform import Rotation as R

from mini_bdx_runtime.hwi import HWI
from mini_bdx_runtime.onnx_infer import OnnxInfer
from mini_bdx_runtime.rl_utils import (
    LowPassActionFilter,
    isaac_to_mujoco,
    make_action_dict,
    mujoco_joints_order,
    mujoco_to_isaac,
    quat_rotate_inverse,
)
from commands_client import CommandsClient


class RLWalk:
    def __init__(
        self,
        onnx_model_path: str,
        serial_port: str = "/dev/ttyUSB0",
        control_freq: float = 60,
        debug_no_imu: bool = False,
        pid=[1000, 0, 500],
        action_scale=0.25,
        cutoff_frequency=100.0,
        commands=False,
        pitch_bias=0.0,
        rma=False,
        adaptation_module_path=None,
    ):
        self.debug_no_imu = debug_no_imu
        self.commands = commands
        self.pitch_bias = pitch_bias

        self.onnx_model_path = onnx_model_path
        self.policy = OnnxInfer(self.onnx_model_path)

        self.rma = rma
        self.num_obs = 51
        if self.rma:
            self.adaptation_module = OnnxInfer(adaptation_module_path, "obs_history")
            self.obs_history_size = 15
            self.obs_history = np.zeros((self.obs_history_size, self.num_obs)).tolist()

        self.hwi = HWI(serial_port)
        if not self.debug_no_imu:
            self.uart = serial.Serial("/dev/ttyS0")  # , baudrate=115200)
            self.imu = adafruit_bno055.BNO055_UART(self.uart)
            # self.imu.mode = adafruit_bno055.NDOF_MODE
            # self.imu.mode = adafruit_bno055.GYRONLY_MODE
            self.imu.mode = adafruit_bno055.IMUPLUS_MODE
            self.last_imu_data = [0, 0, 0, 0]
            self.imu_queue = Queue(maxsize=1)
            Thread(target=self.imu_worker, daemon=True).start()

        self.control_freq = control_freq
        self.pid = pid

        self.linearVelocityScale = 2.0
        self.angularVelocityScale = 0.25
        self.dof_pos_scale = 1.0
        self.dof_vel_scale = 0.05
        self.action_scale = action_scale

        self.prev_action = np.zeros(15)

        self.mujoco_init_pos = list(self.hwi.init_pos.values()) + [0, 0]
        self.isaac_init_pos = np.array(mujoco_to_isaac(self.mujoco_init_pos))

        if self.commands:
            self.commands_client = CommandsClient("192.168.89.246")

        self.action_filter = LowPassActionFilter(self.control_freq, cutoff_frequency)

    def imu_worker(self):
        while True:
            try:
                raw_orientation = self.imu.quaternion  # quat
                euler = R.from_quat(raw_orientation).as_euler("xyz")
            except Exception as e:
                print(e)
                continue

            # Converting to correct axes
            euler = [euler[1], euler[2], euler[0]]
            euler[1] += np.deg2rad(self.pitch_bias)

            final_orientation_quat = R.from_euler("xyz", euler).as_quat()

            self.imu_queue.put(final_orientation_quat)
            time.sleep(1 / (self.control_freq * 2))

    def get_imu_data(self):
        try:
            self.last_imu_data = self.imu_queue.get(False)  # non blocking
        except Exception:
            pass

        return self.last_imu_data

    def get_obs(self, commands):
        if not self.debug_no_imu:
            orientation_quat = self.get_imu_data()
            if orientation_quat is None:
                print("IMU ERROR")
                return None
        else:
            orientation_quat = [1, 0, 0, 0]

        dof_pos = self.hwi.get_present_positions()  # rad
        dof_vel = self.hwi.get_present_velocities()  # rad/s

        dof_pos_scaled = list(
            np.array(dof_pos - self.mujoco_init_pos[:13]) * self.dof_pos_scale
        )
        dof_vel_scaled = list(np.array(dof_vel) * self.dof_vel_scale)

        # adding fake antennas
        dof_pos_scaled = np.concatenate([dof_pos_scaled, [0, 0]])
        dof_vel_scaled = np.concatenate([dof_vel_scaled, [0, 0]])

        dof_pos_scaled = mujoco_to_isaac(dof_pos_scaled)
        dof_vel_scaled = mujoco_to_isaac(dof_vel_scaled)

        projected_gravity = quat_rotate_inverse(orientation_quat, [0, 0, -1])

        return np.concatenate(
            [
                projected_gravity,
                commands,
                dof_pos_scaled,
                dof_vel_scaled,
                self.prev_action,
            ]
        )

    def start(self):
        self.hwi.turn_on()
        self.hwi.set_pid_all(self.pid)

        time.sleep(2)

    def run(self):
        i = 0
        robot_computed_obs = []
        try:
            print("Starting")
            commands = [0.0, 0.0, 0.0]
            while True:
                start = time.time()
                obs = self.get_obs(commands)
                if obs is None:
                    break
                robot_computed_obs.append(obs)

                if self.rma:
                    self.obs_history.append(obs)
                    self.obs_history = self.obs_history[-self.obs_history_size :]
                    latent = self.adaptation_module.infer(
                        np.array(self.obs_history).flatten()
                    )
                    policy_input = np.concatenate([obs, latent])
                    action = self.policy.infer(policy_input)
                else:
                    action = self.policy.infer(obs)

                self.prev_action = action.copy()

                action = action * self.action_scale + self.isaac_init_pos

                self.action_filter.push(action)
                action = self.action_filter.get_filtered_action()

                robot_action = isaac_to_mujoco(action)

                action_dict = make_action_dict(robot_action, mujoco_joints_order)
                self.hwi.set_position_all(action_dict)

                if self.commands:
                    commands = list(
                        np.array(self.commands_client.get_command())
                        * np.array(
                            [
                                self.linearVelocityScale,
                                self.linearVelocityScale,
                                self.angularVelocityScale,
                            ]
                        )
                    )
                    print("commands", commands)

                i += 1
                took = time.time() - start
                # print(
                #     "FPS",
                #     np.around(1 / took, 3),
                #     "-- target",
                #     self.control_freq,
                # )
                # print("===")
                time.sleep((max(1 / self.control_freq - took, 0)))
        except KeyboardInterrupt:
            pass

        pickle.dump(robot_computed_obs, open("robot_computed_obs.pkl", "wb"))
        time.sleep(1)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--onnx_model_path", type=str, required=True)
    parser.add_argument("-a", "--action_scale", type=float, default=0.25)
    parser.add_argument("-p", type=int, default=1000)
    parser.add_argument("-i", type=int, default=0)
    parser.add_argument("-d", type=int, default=500)
    parser.add_argument("-c", "--control_freq", type=int, default=30)
    parser.add_argument("--cutoff_frequency", type=int, default=10)
    parser.add_argument("--rma", action="store_true", default=False)
    parser.add_argument("--adaptation_module_path", type=str, required=False)
    parser.add_argument(
        "--commands",
        action="store_true",
        default=False,
        help="external commands, keyboard or gamepad. Launch control_server.py on host computer",
    )
    parser.add_argument("--pitch_bias", type=float, default=0.0, help="deg")
    args = parser.parse_args()
    pid = [args.p, args.i, args.d]

    rl_walk = RLWalk(
        args.onnx_model_path,
        debug_no_imu=False,
        action_scale=args.action_scale,
        pid=pid,
        control_freq=args.control_freq,
        cutoff_frequency=args.cutoff_frequency,
        commands=args.commands,
        pitch_bias=args.pitch_bias,
        rma=args.rma,
        adaptation_module_path=args.adaptation_module_path,
    )
    rl_walk.start()
    rl_walk.run()
