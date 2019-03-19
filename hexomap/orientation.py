#!/usr/bin/env python
# -*- coding: UTF-8 no BOM -*-

"""
General math module for crystal orientation related calculation.

Most of the conventions used in this module is based on:
    D Rowenhorst et al. 
    Consistent representations of and conversions between 3D rotations
    10.1088/0965-0393/23/8/083501

with the exceptions:
    1. An orientation is always attached to a frame, and all calculations
       between orientations can only be done when all of them are converted
       to the same frame.
    2. Always prefer SI units.
"""

import numpy as np
from dataclasses import dataclass
from hexomap.npmath import norm
from hexomap.npmath import normalize
from hexomap.npmath import random_three_vector


@dataclass
class Eulers:
    """
    Euler angles representation of orientation.

    Euler angle definitions:
        'Bunge' :  z -> x -> z     // prefered
        'Tayt–Briant' x -> y -> z  // roll-pitch-yaw
    """
    phi1: float  # [0, 2pi)
    phi:  float  # [0,  pi]
    phi2: float  # [0, 2pi)
    in_radians: bool=True
    order: str='zxz'
    convention: str='Bunge'

    def __post_init__(self):
        self.phi = self.phi%np.pi 

    @property
    def as_array(self):
        return np.array([self.phi1, self.phi, self.phi2])


@dataclass
class Quaternion:
    """
    Unitary quaternion representation of rotation.
            q = w + x i + y j + z k
    
    reference:
        http://www.euclideanspace.com/maths/algebra/realNormedAlgebra/quaternions/
    
    Note:
        No conversion methods to other representations is provided in this
        class as the conversion requires the knowledge of reference frame,
        whereas quaternion itself does not have a frame (an abstract concept).
    """
    w: float  # cos(theta/2)
    x: float  # sin(theta/2) * rotation_axis_x
    y: float  # sin(theta/2) * rotation_axis_y
    z: float  # sin(theta/2) * rotation_axis_z
    normalized: bool=False

    def __post_init__(self) -> None:
        # standardize the quaternion
        # 1. rotation angle range: [0, pi] -> self.w >= 0
        # 2. |q| === 1
        self.standardize()
    
    def standardize(self) -> None:
        _norm = norm([self.w, self.x, self.y, self.z]) * np.sign(self.w)
        self.w /= _norm
        self.x /= _norm
        self.y /= _norm
        self.z /= _norm
        self.normalized = True

    @property
    def as_array(self) -> np.ndarray:
        return np.array([self.w, self.x, self.y, self.z])

    @property
    def real(self):
        return self.w

    @property
    def imag(self):
        return np.array([self.x, self.y, self.z])

    @property
    def norm(self) -> float:
        return np.linalg.norm(self.as_array)
    
    @property
    def conjugate(self) -> 'Quaternion':
        return Quaternion(self.w, -self.x, -self.y, -self.z)

    def __add__(self, other: 'Quaternion') -> 'Quaternion':
        # NOTE:
        # Adding quaternions has no physical meaning unless the results is
        # averaged to apprixmate the intermedia statem, provided that the
        # two rotations are infinitely small.
        return Quaternion(*(self.as_array + other.as_array))
    
    def __sub__(self, other: 'Quaternion') -> 'Quaternion':
        return Quaternion(*(self.as_array - other.as_array))
    
    def __neg__(self) -> 'Quaternion':
        return Quaternion(*(-self.as_array))

    def __mul__(self, other: 'Quaternion') -> 'Quaternion':
        real = self.real*other.real - np.dot(self.imag, other.imag)
        imag = self.real*other.imag + other.real*self.imag + np.cross(self.imag, other.imag)
        return Quaternion(real, *imag)

    @staticmethod
    def combine_two(q1: 'Quaternion', q2: 'Quaternion') -> 'Quaternion':
        """
        Description
        -----------
        Return the quaternion that represents the compounded rotation, i.e.
            q3 = Quaternion.combine_two(q1, q2)
        where q3 is the single rotation that is equivalent to rotate by q1,
        then by q2.

        Parameters
        ----------
        q1: Quaternion
            first active rotation
        q2: Quaternion
            second active rotation

        Returns
        -------
        Quaternion
            Reduced (single-step) rotation
        """
        return q1*q2

    @staticmethod
    def average_quaternions(qs: list) -> 'Quaternion':
        """
        Description
        -----------
        Return the average quaternion based on algorithm published in
            F. Landis Markley et.al.
            Averaging Quaternions,
            doi: 10.2514/1.28949

        Parameters
        ----------
        qs: list
            list of quaternions for average
        
        Returns
        -------
        Quaternion
            average quaternion of the given list

        Note:
        This method only provides an approximation, with about 1% error. 
        > See the associated unit test for more detials.
        """
        _sum = np.sum([np.outer(q.as_array, q.as_array) for q in qs], axis=0)
        _eigval, _eigvec = np.linalg.eig(_sum/len(qs))
        return Quaternion(*np.real(_eigvec.T[_eigval.argmax()]))

    @staticmethod
    def from_angle_axis(angle: float, axis: np.ndarray) -> 'Quaternion':
        """
        Description
        -----------
        Return a unitary quaternion based on given angle and axis vector

        Parameters
        ----------
        angle: float
            rotation angle in radians (not the half angle omega)
        axis: np.ndarray
            rotation axis
        
        Retruns
        ------
        Quaternion
        """
        axis = normalize(axis)
        return Quaternion(np.cos(angle/2), *(np.sin(angle/2)*axis))

    @staticmethod
    def from_random():
        return Quaternion.from_angle_axis(np.random.random()*np.pi, 
                                          random_three_vector()
                                        )

    @staticmethod
    def quatrotate(q: 'Quaternion', v: np.ndarray) -> np.ndarray:
        """
        Description
        -----------
        Active rotate a given vector v by given unitary quaternion q

        Parameters
        ----------
        q: Quaternion
            quaternion representation of the active rotation
        v: np.ndarray
            vector

        Returns
        -------
        np.ndarray
            rotated vector
        """
        return (q.real**2 - sum(q.imag**2))*v \
            + 2*np.dot(q.imag, v)*q.imag \
            + 2*q.real*np.cross(q.imag, v)


@dataclass
class Frame:
    """
    Reference frame represented as three base vectors
    
    NOTE: in most cases, frames are represented as orthorgonal bases.
    """
    e1: np.ndarray = np.array([1, 0, 0])
    e2: np.ndarray = np.array([0, 1, 0])
    e3: np.ndarray = np.array([0, 0, 1])
    name: str = "lab"


@dataclass
class Orientation:
    """
    Orientation is used to described a given object relative position to the
    given reference frame, more specifically
    
        the orientation of the crystal is described as a passive
        rotation of the sample reference frame to coincide with the 
        crystal’s standard reference frame
    
    """
    _q: Quaternion
    _f: Frame

    @property
    def frame(self) -> 'Frame':
        return self._f
    
    @frame.setter
    def frame(self, new_frame: Frame) -> None:
        pass

    @property
    def as_quaternion(self) -> 'Quaternion':
        return self._q

    @property
    def as_eulers(self) -> 'Eulers':
        pass

    @property
    def as_angleaxis(self) -> tuple:
        pass

    @classmethod
    def random_orientations(cls, n: int, frame: Frame) -> list:
        """Return n random orientations represented in the given frame"""
        return []


def rotate_point(rotation: Quaternion, point: np.ndarray) -> np.ndarray:
    pass
    

if __name__ == "__main__":

    # Example_1:
    #   reudce multi-steps active rotations (unitary quaternions) into a 
    #   single one
    from functools import reduce
    from pprint import pprint
    print("Example_1")
    n_cases = 5
    angs = np.random.random(n_cases) * np.pi
    qs = [Quaternion.from_angle_axis(me, random_three_vector()) for me in angs]
    pprint(qs)
    print("Reduced to:")
    pprint(reduce(Quaternion.combine_two, qs))
    print()

    # Example_2:
    print("Example_2")
    ang = 120
    quat = Quaternion.from_angle_axis(np.radians(ang), np.array([1,1,1]))
    vec = np.array([1,0,0])
    print(f"rotate {vec} by {quat} ({ang} deg) results in:")
    print(Quaternion.quatrotate(quat, vec))
