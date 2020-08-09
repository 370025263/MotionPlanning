import os
import sys
import math
import cvxpy
import numpy as np
import matplotlib.pyplot as plt

sys.path.append(os.path.dirname(os.path.abspath(__file__)) +
                "/../../MotionPlanning/")

import Control.draw as draw
import CurvesGenerator.reeds_shepp as rs
import CurvesGenerator.cubic_spline as cs


class P:
    # System config
    NX = 4  # z = [x, y, v, phi]
    NU = 2  # u = [acceleration, steer]
    T = 6  # finite time horizon length

    # MPC config
    Q = np.diag([1.0, 1.0, 0.5, 0.5])
    Qf = np.diag([1.0, 1.0, 0.5, 0.5])
    R = np.diag([0.01, 0.01])
    Rd = np.diag([0.01, 1.0])
    GOAL_DIS = 1.5  # goal distance
    STOP_SPEED = 0.5 / 3.6  # stop speed
    MAX_TIME = 500.0  # max simulation time
    MAX_ITER = 5  # max iteration
    TARGET_SPEED = 10.0 / 3.6  # target speed
    N_IND_SEARCH = 10  # search index number
    dt = 0.2
    DU_TH = 0.1

    # vehicle config
    RF = 3.3  # [m] distance from rear to vehicle front end of vehicle
    RB = 0.8  # [m] distance from rear to vehicle back end of vehicle
    W = 2.4  # [m] width of vehicle
    WD = 0.7 * W  # [m] distance between left-right wheels
    WB = 2.5  # [m] Wheel base
    TR = 0.44  # [m] Tyre radius
    TW = 0.7  # [m] Tyre width
    MAX_STEER = np.deg2rad(45.0)
    MAX_DSTEER = np.deg2rad(30.0)  # maximum steering speed [rad/s]
    MAX_SPEED = 55.0 / 3.6  # maximum speed [m/s]
    MIN_SPEED = -20.0 / 3.6  # minimum speed [m/s]
    MAX_ACCEL = 1.0  # maximum accel [m/ss]


class Node:
    def __init__(self, x=0.0, y=0.0, yaw=0.0, v=0.0, direct=1.0):
        self.x = x
        self.y = y
        self.yaw = yaw
        self.v = v
        self.direct = direct

    def update(self, a, delta, direct):
        delta = self.limit_input(delta)
        self.x += self.v * math.cos(self.yaw) * P.dt
        self.y += self.v * math.sin(self.yaw) * P.dt
        self.yaw += self.v / P.WB * math.tan(delta) * P.dt
        self.direct = direct
        self.v += self.direct * a * P.dt

    @staticmethod
    def limit_input(delta):
        if delta >= P.MAX_STEER:
            return P.MAX_STEER

        if delta <= -P.MAX_STEER:
            return -P.MAX_STEER

        return delta


class PATH:
    def __init__(self, cx, cy, cyaw, ck):
        self.cx = cx
        self.cy = cy
        self.cyaw = cyaw
        self.ck = ck
        self.length = len(cx)
        self.ind_old = 0

    def nearest_index(self, node):
        dx = [node.x - x for x in self.cx[self.ind_old: (self.ind_old + P.N_IND_SEARCH)]]
        dy = [node.y - y for y in self.cy[self.ind_old: (self.ind_old + P.N_IND_SEARCH)]]
        dist = np.hypot(dx, dy)
        ind = int(np.argmin(dist))
        dist_min = dist[ind]
        ind += self.ind_old
        self.ind_old = ind

        dxl = self.cx[ind] - node.x
        dyl = self.cy[ind] - node.y
        angle = pi_2_pi(self.cyaw[ind] - math.atan2(dyl, dxl))

        if angle < 0.0:
            dist_min *= -1.0

        return ind, dist_min


def pi_2_pi(angle):
    if angle > math.pi:
        return angle - 2.0 * math.pi

    if angle < -math.pi:
        return angle + 2.0 * math.pi

    return angle


def calc_linear_discrete_model(v, phi, delta):
    A = np.array([[1.0, 0.0, P.dt * math.cos(phi), - P.dt * v * math.sin(phi)],
                  [0.0, 1.0, P.dt * math.sin(phi), P.dt * v * math.cos(phi)],
                  [0.0, 0.0, 1.0, 0.0],
                  [0.0, 0.0, P.dt * math.tan(delta) / P.WB, 1.0]])

    B = np.array([[0.0, 0.0],
                  [0.0, 0.0],
                  [P.dt, 0.0],
                  [0.0, P.dt * v / (P.WB * math.cos(delta) ** 2)]])

    C = np.array([P.dt * v * math.sin(phi) * phi,
                  -P.dt * v * math.cos(phi) * phi,
                  0.0,
                  -P.dt * v * delta / (P.WB * math.cos(delta) ** 2)])

    return A, B, C


def calc_speed_profile(cx, cy, cyaw, target_speed):
    speed_profile = [target_speed] * len(cx)
    direction = 1.0  # forward

    # Set stop point
    for i in range(len(cx) - 1):
        dx = cx[i + 1] - cx[i]
        dy = cy[i + 1] - cy[i]

        move_direction = math.atan2(dy, dx)

        if dx != 0.0 and dy != 0.0:
            dangle = abs(pi_2_pi(move_direction - cyaw[i]))
            if dangle >= math.pi / 4.0:
                direction = -1.0
            else:
                direction = 1.0

        if direction != 1.0:
            speed_profile[i] = - target_speed
        else:
            speed_profile[i] = target_speed

    speed_profile[-1] = 0.0

    return speed_profile


def calc_optimal_trajectory_in_T_step(node, ref_path, sp, dl):
    z_opt = np.zeros((P.NX, P.T + 1))
    d_opt = np.zeros((1, P.T + 1))
    length = ref_path.length

    ind, _ = ref_path.nearest_index(node)

    z_opt[0, 0] = ref_path.cx[ind]
    z_opt[1, 0] = ref_path.cy[ind]
    z_opt[2, 0] = sp[ind]
    z_opt[3, 0] = ref_path.cyaw[ind]
    d_opt[0, 0] = 0.0

    travel = 0.0

    for i in range(P.T + 1):
        travel += abs(node.v) * P.dt
        dind = int(round(travel / dl))
        index = min(ind + dind, length - 1)

        z_opt[0, i] = ref_path.cx[index]
        z_opt[1, i] = ref_path.cy[index]
        z_opt[2, i] = sp[index]
        z_opt[3, i] = ref_path.cyaw[index]
        d_opt[0, i] = 0.0

    return z_opt, ind, d_opt


def predict_model(z0, a, delta, z_opt):
    z_bar = z_opt * 0.0

    for i, _ in enumerate(z0):
        z_bar[i, 0] = z0[i]

    node = Node(x=z0[0], y=z0[1], v=z0[2], yaw=z0[3])

    for ai, di, i in zip(a, delta, range(1, P.T + 1)):
        node.update(ai, di, 1.0)
        z_bar[0, i] = node.x
        z_bar[1, i] = node.y
        z_bar[2, i] = node.v
        z_bar[3, i] = node.yaw

    return z_bar


def linear_mpc_control(z_opt, z0, d_opt, oa, od):
    if oa is None or od is None:
        oa = [0.0] * P.T
        od = [0.0] * P.T

    for i in range(P.MAX_ITER):
        z_bar = predict_model(z0, oa, od, z_opt)
        poa, pod = oa[:], od[:]
        oa, od, ox, oy, oyaw, ov = solve_linear_mpc(z_opt, z_bar, z0, d_opt)
        du = sum(abs(oa - poa)) + sum(abs(od - pod))  # calc u change value
        if du <= P.DU_TH:
            break
    else:
        print("Iterative is max iter")

    return oa, od, ox, oy, oyaw, ov


def solve_linear_mpc(z_opt, z_bar, z0, d_opt):
    z = cvxpy.Variable((P.NX, P.T + 1))
    u = cvxpy.Variable((P.NU, P.T))

    cost = 0.0
    constrains = []

    for t in range(P.T):
        cost += cvxpy.quad_form(u[:, t], P.R)

        if t != 0:
            cost += cvxpy.quad_form(z_opt[:, t] - z[:, t], P.Q)

        A, B, C = calc_linear_discrete_model(z_bar[2, t], z_bar[3, t], d_opt[0, t])

        constrains += [z[:, t + 1] == A * z[:, t] + B * u[:, t] + C]

        if t < P.T - 1:
            cost += cvxpy.quad_form(u[:, t + 1] - u[:, t], P.Rd)
            constrains += [cvxpy.abs(u[1, t + 1] - u[1, t]) <= P.MAX_DSTEER * P.dt]

    cost += cvxpy.quad_form(z_opt[:, P.T] - z[:, P.T], P.Qf)

    constrains += [z[:, 0] == z0]
    constrains += [z[2, :] <= P.MAX_SPEED]
    constrains += [z[2, :] >= P.MIN_SPEED]
    constrains += [cvxpy.abs(u[0, :]) <= P.MAX_ACCEL]
    constrains += [cvxpy.abs(u[1, :]) <= P.MAX_STEER]

    prob = cvxpy.Problem(cvxpy.Minimize(cost), constrains)
    prob.solve(solver=cvxpy.OSQP, verbose=False)

    if prob.status == cvxpy.OPTIMAL or \
            prob.status == cvxpy.OPTIMAL_INACCURATE:
        ox = z.value[0, :].flatten()
        oy = z.value[1, :].flatten()
        ov = z.value[2, :].flatten()
        oyaw = z.value[3, :].flatten()
        oa = u.value[0, :].flatten()
        odelta = u.value[1, :].flatten()
    else:
        print("Cannot solve linear mpc")
        oa, odelta, ox, oy, oyaw, ov = None, None, None, None, None, None

    return oa, odelta, ox, oy, oyaw, ov


def main():
    ax = [0.0, 20.0, 40.0, 55.0, 70.0, 85.0]
    ay = [0.0, 50.0, 20.0, 35.0, 0.0, 10.0]
    cx, cy, cyaw, ck, s = cs.calc_spline_course(ax, ay, ds=1.0)
    sp = calc_speed_profile(cx, cy, cyaw, P.TARGET_SPEED)

    ref_path = PATH(cx, cy, cyaw, ck)
    node = Node(x=cx[0], y=cy[0], yaw=cyaw[0], v=0.0)

    time = 0.0
    x = [node.x]
    y = [node.y]
    yaw = [node.yaw]
    v = [node.v]
    t = [0.0]
    d = [0.0]
    a = [0.0]

    target_ind, _ = ref_path.nearest_index(node)

    odelta, oa = None, None

    while time < P.MAX_TIME:
        z_opt, target_ind, d_opt = \
            calc_optimal_trajectory_in_T_step(node, ref_path, sp, 1.0)

        z0 = [node.x, node.y, node.v, node.yaw]
        oa, odelta, ox, oy, oyaw, ov = linear_mpc_control(z_opt, z0, d_opt, oa, odelta)

        if odelta is not None:
            di, ai = odelta[0], oa[0]
        else:
            di, ai = 0.0, 0.0

        node.update(ai, di, 1.0)
        time += P.dt

        x.append(node.x)
        y.append(node.y)
        yaw.append(node.yaw)
        v.append(node.v)
        t.append(time)
        d.append(di)
        a.append(ai)

        dist = math.hypot(node.x - cx[-1], node.y - cy[-1])

        if dist < P.GOAL_DIS and abs(node.v) < P.STOP_SPEED:
            break

        plt.cla()
        plt.gcf().canvas.mpl_connect('key_release_event',
                                     lambda event:
                                     [exit(0) if event.key == 'escape' else None])

        if ox is not None:
            plt.plot(ox, oy, 'xr')

        plt.plot(cx, cy, '-r')
        plt.plot(x, y, '-b')
        plt.plot(z_opt[0, :], z_opt[1, :], 'xk')
        plt.plot(cx[target_ind], cy[target_ind], 'xg')
        plt.axis("equal")
        plt.title("Linear MPC, " + "v = " + str(round(node.v * 3.6, 2)))
        plt.pause(0.001)


if __name__ == '__main__':
    main()