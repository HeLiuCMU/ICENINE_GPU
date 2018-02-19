#!/usr/bin/env python
# implemente simulation code with pycuda
# TODO:
#   optimize hitratio, this part takes most of the reconstruction time.
# He Liu CMU
# 20180117
import cProfile, pstats, StringIO
import pycuda.gpuarray as gpuarray
from pycuda.autoinit import context
import pycuda.driver as cuda
import pycuda.autoinit
import numpy as np
from pycuda.compiler import SourceModule
from pycuda.curandom import MRG32k3aRandomNumberGenerator
import sim_utilities
import RotRep
import IntBin
import FZfile
import time
import random
import scipy.ndimage as ndi
import os.path
import sys
######### import device functions ######
from device_code import mod
########################################
misoren_gpu = mod.get_function("misorien")
def misorien(m0, m1,symMat):
    '''
    calculate misorientation
    :param m0: [n,3,3]
    :param m1: [n,3,3]
    :param symMat: symmetry matrix,
    :return:
    '''
    m0 = m0.reshape([-1,3,3])
    m1 = m1.reshape([-1,3,3])
    symMat = symMat.reshape([-1,3,3])
    if m0.shape != m1.shape:
        raise ValueError(' m0 and m1 should in the same shape')
    NM = m0.shape[0]
    NSymM = symMat.shape[0]
    afMisOrienD = gpuarray.empty([NM,NSymM], np.float32)
    afM0D = gpuarray.to_gpu(m0.astype(np.float32))
    afM1D = gpuarray.to_gpu(m1.astype(np.float32))
    afSymMD = gpuarray.to_gpu(symMat.astype(np.float32))
    misoren_gpu(afMisOrienD, afM0D, afM1D, afSymMD,block=(NSymM,1,1),grid=(NM,1))
    #print(symMat[0])
    #print(symMat[0].dot(np.matrix(m1)))
    return np.amin(afMisOrienD.get(), axis=1)

class Reconstructor_GPU():
    '''
    example usage:
    Todo:
        load voxelpos only once, not like now, copy every time
        implement random matrix in GPU! high priority
        flood filling problem, this kind of filling will actually affecto the shape of grain,
        voxel at boundary need to compare different grains.

    '''
    def __init__(self):
        self.squareMicOutFile = 'DefaultReconOutPut.npy'
        self.FZFile = '/home/heliu/work/I9_test_data/FIT/DataFiles/HexFZ.dat'
        self.expDataInitial = '/home/heliu/work/I9_test_data/Integrated/S18_z1_'
        self.expdataNDigit = 6
        # initialize voxel position information
        self.voxelpos = np.array([[-0.0953125, 0.00270633, 0]])    # nx3 array, voxel positions
        self.NVoxel = self.voxelpos.shape[0]                       # number of voxels
        self.voxelAcceptedMat = np.zeros([self.NVoxel,3,3])        # reconstruced rotation matrices
        self.voxelHitRatio = np.zeros(self.NVoxel)                 # reconstructed hit ratio
        self.voxelIdxStage0 = range(self.voxelpos.shape[0])       # this contains index of the voxel that have not been tried to be reconstructed, used for flood fill process
        self.voxelIdxStage1 = []                    # this contains index  the voxel that have hit ratio > threshold on reconstructed voxel, used for flood fill process
        self.micData = np.zeros([self.NVoxel,11])                  # mic data loaded from mic file, get with self.load_mic(fName), detail format see in self.load_mic()
        self.FZEuler = np.array([[89.5003, 80.7666, 266.397]])     # fundamental zone euler angles, loaded from I9 fz file.
        self.oriMatToSim = np.zeros([self.NVoxel,3,3])             # the orientation matrix for simulation, nx3x3 array, n = Nvoxel*OrientationPerVoxel
        # experimental data
        self.energy = 55.587 # in kev
        self.sample = sim_utilities.CrystalStr('Ti7') # one of the following options:
        self.maxQ = 8
        self.etalimit = 81 / 180.0 * np.pi
        self.detectors = [sim_utilities.Detector(),sim_utilities.Detector()]
        self.NRot = 180
        self.NDet = 2
        self.detScale = 0.25  # the pixel size will be 1/self.detScale and NPixelJ = NPixelJ*self.detScale
        self.centerJ = [976.072*self.detScale,968.591*self.detScale]# center, horizental direction
        self.centerK = [2014.13*self.detScale,2011.68*self.detScale]# center, verticle direction
        self.detPos = [np.array([5.46569,0,0]),np.array([7.47574,0,0])] # in mm
        self.detRot = [np.array([91.6232, 91.2749, 359.274]),np.array([90.6067, 90.7298, 359.362])]# Euler angleZXZ
        self.detectors[0].NPixelJ = int(2048*self.detScale)
        self.detectors[0].NPixelK = int(2048*self.detScale)
        self.detectors[0].PixelJ = 0.00148/self.detScale
        self.detectors[0].PixelK = 0.00148/self.detScale
        self.detectors[1].NPixelJ = int(2048*self.detScale)
        self.detectors[1].NPixelK = int(2048*self.detScale)
        self.detectors[1].PixelJ = 0.00148/self.detScale
        self.detectors[1].PixelK = 0.00148/self.detScale

        self.detectors[0].Move(self.centerJ[0], self.centerK[0], self.detPos[0], RotRep.EulerZXZ2Mat(self.detRot[0] / 180.0 * np.pi))
        self.detectors[1].Move(self.centerJ[1], self.centerK[1], self.detPos[1], RotRep.EulerZXZ2Mat(self.detRot[1] / 180.0 * np.pi))

        #detinfor for GPU[0:NJ,1:JK,2:pixelJ, 3:pixelK, 4-6: coordOrigin, 7-9:Norm 10-12 JVector, 13-16: KVector, 17: NRot, 18: angleStart, 19: angleEnd
        lDetInfoTmp = []
        for i in range(self.NDet):
            lDetInfoTmp.append(np.concatenate([np.array([self.detectors[i].NPixelJ,self.detectors[i].NPixelK,
                                                         self.detectors[i].PixelJ,self.detectors[i].PixelK]),
                                                self.detectors[i].CoordOrigin,self.detectors[i].Norm,self.detectors[i].Jvector,
                                               self.detectors[i].Kvector,np.array([self.NRot,-np.pi/2,np.pi/2])]))
        self.afDetInfoH = np.concatenate(lDetInfoTmp)

        # initialize Scattering Vectors
        self.sample.getRecipVec()
        self.sample.getGs(self.maxQ)
        self.NG = self.sample.Gs.shape[0]
        self.symMat = RotRep.GetSymRotMat('Hexagonal')

        # reconstruction parameters:
        self.floodFillStartThreshold = 0.61 # orientation with hit ratio larger than this value is used for flood fill.
        self.floodFillSelectThreshold = 0.6 # voxels with hitratio less than this value will be reevaluated in flood fill process.
        self.floodFillAccptThreshold = 0.6  #voxel with hit ratio > floodFillTrheshold will be accepted to voxelIdxStage1
        self.floodFillRandomRange = 0.005   # voxel in fill process will generate random angles in this window
        self.floodFillNumberAngle = 1000 # number of rangdom angles generated to voxel in voxelIdxStage1
        self.floodFillNumberVoxel = 20000  # number of orientations for flood fill process each time, due to GPU memory size.
        self.floodFillNIteration = 2       # number of iteration for flood fill angles
        self.searchBatchSize = 20000      # number of orientations to search per GPU call, due to GPU memory size
        self.NSelect = 100                 # number of orientations selected with maximum hitratio from last iteration
        self.postMisOrienThreshold = 0.02  # voxel with misorientation larger than this will be post processed seeds voxel
        self.postWindow = 3                #voxels in the nxn window around the seeds voxels selected above will be processed
        self.postRandomRange = 0.001       # the random angle range generated in post process
        self.postConvergeMisOrien = 0.01   # if the misorientation to the same voxel from last iteration of post process less than this value, considered converge
        self.postNRandom = 50             # number of random angle generated in each orientation seed
        self.postOriSeedWindow = 4         # orienatation in this nxn window around the voxel will be used as seed to generate raondom angle.
        self.postNIteration = 1            # number of iteration to optimize in post process
        self.expansionStopHitRatio = 0.5    # when continuous 2 voxel hitratio below this value, voxels outside this region will not be reconstructed
        # retrieve gpu kernel
        self.sim_func = mod.get_function("simulation")
        self.hitratio_func = mod.get_function("hitratio_multi_detector")
        self.mat_to_euler_ZXZ = mod.get_function("mat_to_euler_ZXZ")
        self.rand_mat_neighb_from_euler = mod.get_function("rand_mat_neighb_from_euler")
        self.euler_zxz_to_mat_gpu = mod.get_function("euler_zxz_to_mat")
        self.sim_hitratio_unit = mod.get_function("sim_hitratio_unit")
        # GPU random generator
        self.randomGenerator = MRG32k3aRandomNumberGenerator()
        # initialize device parameters and outputs
        #self.afGD = gpuarray.to_gpu(self.sample.Gs.astype(np.float32))
        # initialize tfG
        self.tfG = mod.get_texref("tfG")
        self.tfG.set_array(cuda.np_to_array(self.sample.Gs.astype(np.float32),order='C'))
        self.tfG.set_flags(cuda.TRSA_OVERRIDE_FORMAT)
        print(self.sample.Gs.shape)
        self.afDetInfoD = gpuarray.to_gpu(self.afDetInfoH.astype(np.float32))

    def set_det(self):
        del self.afDetInfoD
        del self.afDetInfoH
        self.detectors = [sim_utilities.Detector(), sim_utilities.Detector()]
        self.detectors[0].NPixelJ = int(2048*self.detScale)
        self.detectors[0].NPixelK = int(2048*self.detScale)
        self.detectors[0].PixelJ = 0.00148/self.detScale
        self.detectors[0].PixelK = 0.00148/self.detScale
        self.detectors[1].NPixelJ = int(2048*self.detScale)
        self.detectors[1].NPixelK = int(2048*self.detScale)
        self.detectors[1].PixelJ = 0.00148/self.detScale
        self.detectors[1].PixelK = 0.00148/self.detScale
        self.detectors[0].Move(self.centerJ[0], self.centerK[0], self.detPos[0], RotRep.EulerZXZ2Mat(self.detRot[0] / 180.0 * np.pi))
        self.detectors[1].Move(self.centerJ[1], self.centerK[1], self.detPos[1], RotRep.EulerZXZ2Mat(self.detRot[1] / 180.0 * np.pi))
        #detinfor for GPU[0:NJ,1:JK,2:pixelJ, 3:pixelK, 4-6: coordOrigin, 7-9:Norm 10-12 JVector, 13-16: KVector, 17: NRot, 18: angleStart, 19: angleEnd
        lDetInfoTmp = []
        for i in range(self.NDet):
            lDetInfoTmp.append(np.concatenate([np.array([self.detectors[i].NPixelJ,self.detectors[i].NPixelK,
                                                         self.detectors[i].PixelJ,self.detectors[i].PixelK]),
                                                self.detectors[i].CoordOrigin,self.detectors[i].Norm,self.detectors[i].Jvector,
                                               self.detectors[i].Kvector,np.array([self.NRot,-np.pi/2,np.pi/2])]))
        self.afDetInfoH = np.concatenate(lDetInfoTmp)
        self.afDetInfoD = gpuarray.to_gpu(self.afDetInfoH.astype(np.float32))
    def recon_prepare(self):
        # prepare nessasary parameters
        self.load_fz(self.FZFile)
        self.load_exp_data(self.expDataInitial, self.expdataNDigit)
        self.expData[:, 2:4] = self.expData[:, 2:4] * self.detScale  # half the detctor size, to rescale real data
        #self.expData = np.array([[1,2,3,4]])
        self.cp_expdata_to_gpu()
        #self.create_acExpDataCpuRam()
        # setup serial Reconstruction rotMatCandidate
        self.FZMatH = np.empty([self.searchBatchSize,3,3])
        if self.searchBatchSize > self.FZMat.shape[0]:
            self.FZMatH[:self.FZMat.shape[0], :, :] = self.FZMat
            self.FZMatH[self.FZMat.shape[0]:,:,:] = FZfile.generate_random_rot_mat(self.searchBatchSize - self.FZMat.shape[0])
        else:
            raise ValueError(" search batch size less than FZ file size, please increase search batch size")

        # initialize device parameters and outputs
        #self.afGD = gpuarray.to_gpu(self.sample.Gs.astype(np.float32))
        self.afDetInfoD = gpuarray.to_gpu(self.afDetInfoH.astype(np.float32))
        self.afFZMatD = gpuarray.to_gpu(self.FZMatH.astype(np.float32))          # no need to modify during process

    def geometry_optimizer(self,aL,aJ, aK, aDetRot,
                           relativeL=0.05,relativeJ=15, relativeK=5,
                           rate = 1,NIteration=30,factor=0.85, NStep=10, geoSearchNVoxel=1,
                           lVoxel=None, searchMatD=None, NSearchOrien=None,
                           NOrienIteration=10, BoundStart=0.5):
        '''
        optimize the geometry of
        kind of similar to Coordinate Gradient Descent
        stage: fix relative distance,do not search for rotation
            search K  most sensitive
            search L: step should be less than 0.5mm
            search J: lest sensitive
            repeat  in order K,L,J, shrink search range.
        procedure, start with large search range, set rate=1, others as default, if it end up with hitratio>0.65, reduce
        rate to 0.1, else repeat at rate=1. after hitratio>0.75, can be accepted.
        Todo:
            select grain boundary voxels and use them for parameter optimization.

        :param aL:  [NStep, NDet]
        :param aJ:[NStep, NDet]
        :param aK:[NStep, NDet]
        :param aDetRot:[NStep, NDet,3]
        :param factor: the search range will shrink by factor after each iteration
        :param relativeJ: search range of relative J between different detectors
        :param relativeK: search range of relative K between different detectors
        :param rate: similar to learning rate
        :param NIteration: number of iterations for searching parameters
        :param NStep: number of uniform points in search range
        :param geoSearchNVoxel: number of voxels for searching parameters
        :return:

        optimize the geometry of
        stage1: fix relative distance,do not search for rotation
            search K  most sensitive
            search L: step should be less than 0.5mm
            search J: lest sensitive
            repeat  in order K,L,J, shrink search range.
        :return:
        '''
        if lVoxel is None:
            x = np.arange(int(0.1*self.squareMicData.shape[0]), int(0.9*self.squareMicData.shape[0]), 1)
            y = np.arange(int(0.1*self.squareMicData.shape[1]), int(0.9*self.squareMicData.shape[1]), 1)
            lVoxel = x * self.squareMicData.shape[0] + y
        if searchMatD is None:
            searchMatD = self.afFZMatD
        if NSearchOrien is None:
            NSearchOrien = self.searchBatchSize
        if self.NDet!=2:
            raise ValueError('currently this function only support 2 detectors')
        sys.stdout.flush()
        L = aL[aL.shape[0]//2,:].reshape([1,self.NDet])
        J = aJ[aJ.shape[0]//2,:].reshape([1,self.NDet])
        K = aK[aK.shape[0]//2,:].reshape([1,self.NDet])
        rot = aDetRot[aDetRot.shape[0]//2,:,:].reshape([1,self.NDet,3])
        rangeL = (aL[:, 0].max() - aL[:, 0].min()) / 2
        rangeJ = (aJ[:, 0].max() - aJ[:, 0].min()) / 2
        rangeK = (aK[:, 0].max() - aK[:, 0].min()) / 2
        #NIteration = 100
        #factor = 0.8
        maxHitRatioPre = 0
        maxHitRatio = 0
        for i in range(NIteration):
            #x = np.random.choice(np.arange(10, 90, 1), geoSearchNVoxel)
            #y = np.random.choice(np.arange(10, 90, 1), geoSearchNVoxel)
            #lVoxelIdx = x * self.squareMicData.shape[0] + y
            lVoxelIdx = np.random.choice(lVoxel, geoSearchNVoxel)
            #update both K
            self.geometry_grid_search(L, J, aK,rot,lVoxelIdx,searchMatD,NSearchOrien,NOrienIteration, BoundStart)
            # maxHitRatioPre = min(maxHitRatio, 0.7)
            maxHitRatio = self.geoSearchHitRatio.max()
            # if(maxHitRatio>maxHitRatioPre):
            K = (1- rate*maxHitRatio**3)*K + (rate*maxHitRatio**3)*aK[np.argmax(self.geoSearchHitRatio.ravel()),:].reshape([1,self.NDet])
            print('update K to {0}, max hitratio is  {1}'.format(K, maxHitRatio))
            #update both L
            self.geometry_grid_search(aL, J, K, rot, lVoxelIdx,searchMatD,NSearchOrien,NOrienIteration, BoundStart)
            # maxHitRatioPre = min(maxHitRatio, 0.7)
            maxHitRatio = self.geoSearchHitRatio.max()
            # if(maxHitRatio>maxHitRatioPre):
            L = (1-rate*maxHitRatio**3)*L + (rate*maxHitRatio**3)*aL[np.argmax(self.geoSearchHitRatio.ravel()),:].reshape([1,self.NDet])
            print('update L to {0}, max hitratio is  {1}'.format(L, maxHitRatio))
            #update both J
            self.geometry_grid_search(L, aJ, K, rot, lVoxelIdx,searchMatD,NSearchOrien,NOrienIteration, BoundStart)
            # maxHitRatioPre = min(maxHitRatio, 0.7)
            maxHitRatio = self.geoSearchHitRatio.max()
            # if(maxHitRatio>maxHitRatioPre):
            J = (1-rate*maxHitRatio**3)*J + (rate*maxHitRatio**3)*aJ[np.argmax(self.geoSearchHitRatio.ravel()),:].reshape([1,self.NDet])
            print('update J to {0}, max hitratio is  {1}'.format(J, maxHitRatio))
            dL = np.zeros([NStep,self.NDet])
            dL[:,1] = np.linspace(-relativeL, relativeL, NStep)
            dJ = np.zeros([NStep,self.NDet])
            dJ[:,1] = np.linspace(-relativeJ, relativeJ, NStep)
            dK = np.zeros([NStep,self.NDet])
            dK[:,1] = np.linspace(-relativeK, relativeK, NStep)

            aL = L.repeat(dL.shape[0], axis=0) + dL
            aJ = J.repeat(dJ.shape[0], axis=0) + dJ
            aK = K.repeat(dK.shape[0], axis=0) + dK
            # update relative K
            self.geometry_grid_search(L, J, aK, rot, lVoxelIdx,searchMatD,NSearchOrien,NOrienIteration, BoundStart)
            # maxHitRatioPre = min(maxHitRatio, 0.7)
            maxHitRatio = self.geoSearchHitRatio.max()
            # if(maxHitRatio>maxHitRatioPre):
            K = (1 - rate*maxHitRatio ** 3) * K + (rate*maxHitRatio ** 3) * aK[np.argmax(self.geoSearchHitRatio.ravel()), :].reshape(
                [1, self.NDet])
            print('update K to {0}, max hitratio is  {1}'.format(K, maxHitRatio))
            # update relative L
            self.geometry_grid_search(aL, J, K, rot, lVoxelIdx,searchMatD,NSearchOrien,NOrienIteration, BoundStart)
            # maxHitRatioPre = min(maxHitRatio, 0.7)
            maxHitRatio = self.geoSearchHitRatio.max()
            # if(maxHitRatio>maxHitRatioPre):
            L = (1 - rate*maxHitRatio ** 3) * L + (rate*maxHitRatio ** 3) * aL[np.argmax(self.geoSearchHitRatio.ravel()), :].reshape(
                [1, self.NDet])
            print('update L to {0}, max hitratio is  {1}'.format(L, maxHitRatio))
            # update relative J
            self.geometry_grid_search(L, aJ, K, rot, lVoxelIdx,searchMatD,NSearchOrien,NOrienIteration, BoundStart)
            # maxHitRatioPre = min(maxHitRatio, 0.7)
            maxHitRatio = self.geoSearchHitRatio.max()
            # if(maxHitRatio>maxHitRatioPre):
            J = (1 - rate*maxHitRatio ** 3) * J + (rate*maxHitRatio ** 3) * aJ[np.argmax(self.geoSearchHitRatio.ravel()), :].reshape(
                [1, self.NDet])
            print('update J to {0}, max hitratio is  {1}'.format(J, maxHitRatio))
            # update relative Range
            rangeL = rangeL * factor
            rangeJ = rangeJ * factor
            rangeK = rangeK * factor
            relativeJ *= factor
            relativeK *= factor
            print(rangeL, rangeJ, rangeK)
            dL = np.linspace(-rangeL, rangeL, NStep).reshape([-1, 1]).repeat(self.NDet, axis=1)
            dJ = np.linspace(-rangeJ, rangeJ, NStep).reshape([-1, 1]).repeat(self.NDet, axis=1)
            dK = np.linspace(-rangeK, rangeK, NStep).reshape([-1, 1]).repeat(self.NDet, axis=1)
            aL = L.repeat(dL.shape[0], axis=0) + dL
            aJ = J.repeat(dJ.shape[0], axis=0) + dJ
            aK = K.repeat(dK.shape[0], axis=0) + dK
        # for idxDet in range(self.NDet):
        #     self.detPos[idxDet][0] = L[0, idxDet]
        #     self.centerJ[idxDet] = J[0, idxDet]
        #     self.centerK[idxDet] = K[0, idxDet]
        #     self.detRot[idxDet] = rot[0, idxDet]
        # self.set_det()
        return L,J,K,rot
        print('new L: {0}, new J: {1}, new K: {2}, new rot: {3}'.format(L, J, K, rot))

    def geometry_grid_search(self,aL,aJ, aK, aDetRot, lVoxelIdx, searchMatD, NSearchOrien,NIteration=10, BoundStart=0.5):
        '''
        optimize geometry of HEDM setup
        stragegy:
            1.fix ralative L and fix detector rotation, change L, j, and k
        aL: nxNDet
        aDetRot = [n,NDet,3]
        :return:
        '''
        ############## part 1 ############
        # start from optimal L, and test how L affects hitratio:
        if aL.ndim!=2 or aJ.ndim!=2 or aK.ndim!=2 or aDetRot.ndim!=3:
            raise ValueError('input should be [n,NDet] array')
        elif aL.shape[1]!=self.NDet or aJ.shape[1]!=self.NDet or aK.shape[1]!=self.NDet or aDetRot.shape[1]!=self.NDet:
            raise ValueError('input should be in shape [n,NDet]')
        #lVoxelIdx = [self.squareMicData.shape[0]*self.squareMicData.shape[1]/2 + self.squareMicData.shape[1]/2]

        self.geoSearchHitRatio = np.zeros([aL.shape[0], aJ.shape[0], aK.shape[0],aDetRot.shape[0]])
        for idxl in range(aL.shape[0]):
            for idxJ in range(aJ.shape[0]):
                for idxK in range(aK.shape[0]):
                    for idxRot in range(aDetRot.shape[0]):
                        for idxDet in range(self.NDet):
                            self.detPos[idxDet][0] = aL[idxl,idxDet]
                            self.centerJ[idxDet] = aJ[idxJ,idxDet]
                            self.centerK[idxDet] = aK[idxK, idxDet]
                            self.detRot[idxDet] = aDetRot[idxRot, idxDet]
                        self.set_det()
                        for voxelIdx in lVoxelIdx:
                            self.single_voxel_recon(voxelIdx,searchMatD, NSearchOrien,NIteration, BoundStart)
                            self.geoSearchHitRatio[idxl,idxJ,idxK,idxRot] += self.voxelHitRatio[voxelIdx]
                        self.geoSearchHitRatio[idxl, idxJ, idxK,idxRot] = self.geoSearchHitRatio[idxl, idxJ, idxK,idxRot] / len(lVoxelIdx)
        #print(self.geoSearchHitRatio)
        return self.geoSearchHitRatio
    def hitratio_cpu(self, aJ, aK, aRotN, aHit, NVoxel, NOrientation):
        '''
        This is not successful, GPU: 0.03, CPU: 0.9
        :param aJ: iNVoxel*iNOrientation*iNG*2*iDet ,2 is for omega1 and omega2
        :param aK:
        :param aRotN:
        :param aHit:
        :return: hitRatio, hitPeakCnt, iNVoxel*iNOrientation
        '''
        aJ = aJ.reshape([NVoxel*NOrientation, self.NG * 2, self.NDet])
        aK = aK.reshape([NVoxel*NOrientation, self.NG * 2, self.NDet])
        aRotN = aRotN.reshape([NVoxel*NOrientation, self.NG * 2, self.NDet])
        aHit = aHit.reshape([NVoxel*NOrientation, self.NG * 2, self.NDet])
        result = np.ones([NVoxel * NOrientation, self.NG * 2])
        simHit = np.ones([NVoxel * NOrientation, self.NG * 2])==1
        for i in range(self.NDet):
            simHit = np.logical_and(simHit, aHit[:,:,i])

        for i in range(self.NDet):
            tmp = np.zeros([NVoxel*NOrientation, self.NG * 2])
            imgIdx = aJ[:,:,i][simHit] + aK[:,:,i][simHit] * self.detectors[i].NPixelJ \
                                      +  aRotN[:,:,i][simHit] * self.detectors[i].NPixelK * self.detectors[i].NPixelJ \
                                     + self.aiDetStartIdxH[i]
            tmp[simHit] = self.acExpDataCpuRam[imgIdx]
            result = result * tmp
        overlaped = np.sum(result,axis=1)
        peakCnt = np.sum(simHit, axis=1)
        hitRatio = overlaped.astype(np.float32)/peakCnt
        return hitRatio, peakCnt

    def create_acExpDataCpuRam(self):
        # for testing cpu verison of hitratio
        # require have defiend self.NDet,self.NRot, and Detctor informations;
        #self.expData = np.array([[0,24,324,320],[0,0,0,1]]) # n_Peak*3,[detIndex,rotIndex,J,K] !!! be_careful this could go wrong is assuming wrong number of detectors
        #self.expData = np.array([[0,24,648,640],[0,172,285,631],[1,24,720,485],[1,172,207,478]]) #[detIndex,rotIndex,J,K]
        print('=============start of copy exp data to CPU ===========')
        if self.expData.shape[1]!=4:
            raise ValueError('expdata shape should be n_peaks*4')
        if np.max(self.expData[:,0])>self.NDet-1:
            raise ValueError('expData contains detector index out of bound')
        if np.max(self.expData[:,1])>self.NRot-1:
            raise  ValueError('expData contaisn rotation number out of bound')
        self.aiDetStartIdxH = [0] # index of Detctor start postition in self.acExpDetImages, e.g. 3 detectors with size 2048x2048, 180 rotations, self.aiDetStartIdx = [0,180*2048*2048,2*180*2048*2048]
        self.iExpDetImageSize = 0
        for i in range(self.NDet):
            self.iExpDetImageSize += self.NRot*self.detectors[i].NPixelJ*self.detectors[i].NPixelK
            if i<(self.NDet-1):
                self.aiDetStartIdxH.append(self.iExpDetImageSize)
        # check is detector size boyond the number int type could hold
        if self.iExpDetImageSize<0 or self.iExpDetImageSize>2147483647:
            raise ValueError("detector image size {0} is wrong, \n\
                             possible too large detector size\n\
                            currently use int type as detector pixel index\n\
                            future implementation use lognlong will solve this issure")

        self.aiDetStartIdxH = np.array(self.aiDetStartIdxH)
        self.acExpDataCpuRam = np.zeros((self.iExpDetImageSize,),dtype=np.int8)
        self.iNPeak = np.int32(self.expData.shape[0])
        for i in range(self.iNPeak):
            self.acExpDataCpuRam[self.aiDetStartIdxH[self.expData[i, 0]] \
                                + self.expData[i, 1] * self.detectors[self.expData[i, 0]].NPixelK * self.detectors[self.expData[i, 0]].NPixelJ \
                                + self.expData[i, 3] * self.detectors[self.expData[i, 0]].NPixelJ \
                                + self.expData[i, 2]] = 1


        print('=============end of copy exp data to CPU ===========')
    def post_process(self):
        '''
        In this process, voxels with misorientation to its  neighbours greater than certain level will be revisited,
        new candidate orientations are taken from the neighbours around this voxel, random orientations will be generated
        based on these candidate orientations. this process repeats until no orientations of these grain boundary voxels
        does not change anymore.
        need load squareMicData
        need self.voxelAccptMat
        :return:
        '''
        ############# test section #####################
        # self.load_exp_data('/home/heliu/work/I9_test_data/Integrated/S18_z1_', 6)
        # self.expData[:, 2:4] = self.expData[:, 2:4] / 4  # half the detctor size, to rescale real data
        # self.cp_expdata_to_gpu()
        ############### test section edn ###################

        NVoxelX = self.squareMicData.shape[0]
        NVoxelY = self.squareMicData.shape[1]
        accMat = self.voxelAcceptedMat.copy().reshape([NVoxelX, NVoxelY, 9])

        misOrienTmp = self.get_misorien_map(accMat)
        self.NPostProcess = 0
        self.NPostVoxelVisited = 0
        start = time.time()
        while np.max(misOrienTmp) > self.postConvergeMisOrien:
            print(np.max(misOrienTmp))
            self.NPostProcess += 1
            misOrienTmp = ndi.maximum_filter(misOrienTmp, self.postWindow) * (self.voxelHitRatio>0).reshape([NVoxelX,NVoxelY]) # due to expansion mode
            x, y = np.where(np.logical_and(misOrienTmp > self.postMisOrienThreshold, self.squareMicData[:, :, 7] == 1))
            xMin = np.minimum(np.maximum(0, x - (self.postOriSeedWindow-1)//2), NVoxelX - self.postOriSeedWindow)
            yMin = np.minimum(np.maximum(0, y - (self.postOriSeedWindow-1)//2), NVoxelY - self.postOriSeedWindow)
            aIdx = x * NVoxelY + y
            for i, idx in enumerate(aIdx):
                self.NPostVoxelVisited += 1
                rotMatSeed = accMat[xMin[i]:xMin[i]+self.postOriSeedWindow,yMin[i]:yMin[i]+self.postOriSeedWindow,:].astype(np.float32)
                rotMatSeedD = gpuarray.to_gpu(rotMatSeed)
                rotMatSearchD = self.gen_random_matrix(rotMatSeedD,self.postOriSeedWindow**2,self.postNRandom,self.postRandomRange)
                self.single_voxel_recon(idx,rotMatSearchD,self.postNRandom * self.postOriSeedWindow ** 2,
                                        NIteration=self.postNIteration, BoundStart=self.postRandomRange)
            accMatNew = self.voxelAcceptedMat.copy().reshape([NVoxelX, NVoxelY, 9])
            misOrienTmpNew = misorien(accMatNew, accMat, self.symMat).reshape([NVoxelX,NVoxelY])
            misOrienTmp = misOrienTmpNew.copy()* (self.voxelHitRatio>0).reshape([NVoxelX,NVoxelY])
            accMat = accMatNew.copy()
            print('max misorien: {0}'.format(np.max(misOrienTmp)))
        print('number of post process iteration: {0}, number of voxel revisited: {1}'.format(self.NPostProcess,self.NPostVoxelVisited))
        end = time.time()
        print(' post process takes is {0} seconds'.format(end-start))
        # self.squareMicData[:,:,3:6] = (RotRep.Mat2EulerZXZVectorized(self.voxelAcceptedMat)/np.pi*180).reshape([self.squareMicData.shape[0],self.squareMicData.shape[1],3])
        # self.squareMicData[:,:,6] = self.voxelHitRatio.reshape([self.squareMicData.shape[0],self.squareMicData.shape[1]])
        # self.save_square_mic('SquareMicTest2_postprocess.npy')

    def get_misorien_map(self,m0):
        '''
        map the misorienation map
        e.g. a 100x100 square voxel will give 99x99 misorientations if axis=0,
        but it will still return 100x100, filling 0 to the last row/column
        the misorientatino on that voxel is the max misorientation to its right or up side voxel
        :param axis: 0 for x direction, 1 for y direction.
        :return:
        '''
        if m0.ndim<3:
            raise ValueError('input should be [nvoxelx,nvoxely,9] matrix')
        NVoxelX = m0.shape[0]
        NVoxelY = m0.shape[1]
        #m0 = self.voxelAcceptedMat.reshape([NVoxelX, NVoxelY, 9])
        m1 = np.empty([NVoxelX, NVoxelY, 9])
        # x direction misorientatoin
        m1[:-1,:,:] = m0[1:,:,:]
        m1[-1,:,:] = m0[-1,:,:]
        misorienX = misorien(m0, m1, self.symMat)
        # y direction misorientation
        m1[:,:-1,:] = m0[:,1:,:]
        m1[:,-1,:] = m0[:,-1,:]
        misorienY = misorien(m0, m1, self.symMat)
        self.misOrien = np.maximum(misorienX, misorienY).reshape([NVoxelX, NVoxelY])
        return self.misOrien

    def set_voxel_pos(self,pos,mask=None):
        '''
        set voxel positions as well as mask
        :param pos: shape=[n_voxel,3] , in form of [x,y,z]
        :return:
        '''
        self.voxelpos = pos.reshape([-1,3])  # nx3 array,
        self.NVoxel = self.voxelpos.shape[0]
        self.voxelAcceptedMat = np.zeros([self.NVoxel, 3, 3])
        self.voxelHitRatio = np.zeros(self.NVoxel)
        if mask is None:
            self.voxleMask = np.ones(self.NVoxel)
        elif mask.size==self.NVoxel:
            self.voxelMask = mask.ravel()
        else:
            raise ValueError(' mask should have the same number of voxel as self.voxelpos')
        self.voxelIdxStage0 = list(np.where(self.voxelMask==1)[0])   # this contains index of the voxel that have not been tried to be reconstructed, used for flood fill process
        self.voxelIdxStage1 = []                                # this contains index  the voxel that have hit ratio > threshold on reconstructed voxel, used for flood fill process
        print("voxelpos shape is {0}".format(self.voxelpos.shape))

    def create_square_mic(self,shape=(100,100), shift=[0,0,0], voxelsize=0.01,mask=None):
        '''
        initialize a square mic file
        Currently, the image is treated start from lower left corner, x is horizental direction, y is vertical, X-ray comes from -y shoot to y
        output mic format: [NVoxelX,NVoxleY,10]
        each Voxel conatains 10 columns:
            0-2: voxelpos [x,y,z]
            3-5: euler angle
            6: hitratio
            7: maskvalue. 0: no need for recon, 1: active recon region
            8: voxelsize
            9: additional information
        :param shape: array like [NVoxelX,NVxoelY]
        :param shift: arraylike, [dx,dy,dz]
        :param voxelsize: in mm
        :param mask:
        :return:
        '''
        shape = tuple(shape)
        if len(shape)!=2:
            raise ValueError(' input shape should be in the form [x,y]')
        if mask is None:
            mask = np.ones(shape)
        if mask.shape!= shape:
            raise ValueError('mask should be in the same shape as input')
        shift = np.array(shift).ravel()
        if shift.size!=3:
            raise ValueError(' shift size should be 3, (dx,dy,dz)')
        self.squareMicData = np.zeros([shape[0],shape[1],10])
        self.squareMicData[:,:,7] = mask[:,:]
        self.squareMicData[:,:,8] = voxelsize
        midVoxel = np.array([float(shape[0]) / 2, float(shape[1]) / 2, 0])
        for ii in range(self.squareMicData.shape[0]):
            for jj in range(self.squareMicData.shape[1]):
                self.squareMicData[ii,jj,0:3] = (np.array([ii+0.5, jj+0.5, 0])-midVoxel)*voxelsize + shift
        self.set_voxel_pos(self.squareMicData[:,:,:3].reshape([-1,3]), self.squareMicData[:,:,7].ravel())

    def save_square_mic(self, fName, format='npy'):
        '''
        save square mic data
        :param format: 'npy' or 'txt'
        :return:
        '''
        if format=='npy':
            np.save(fName, self.squareMicData)
            print('saved as npy format')
        elif format=='txt':
            print('not implemented')
            pass
            # np.savetxt(fName,self.squareMicData.reshape([-1,10]),header=str(self.squareMicData.shape))
            # print('saved as txt format')
        else:
            raise ValueError('format could only be npy or txt')

    def load_square_mic(self, fName, format='npy'):
        if format=='npy':
            self.squareMicData = np.load(fName)
            print('saved as npy format')
        elif format=='txt':
            print('not implemented')
            pass
            # self.squareMicData = np.loadtxt(fName)
            # print('saved as txt format')
        else:
            raise ValueError('format could only be npy or txt')
        self.set_voxel_pos(self.squareMicData[:, :, :3].reshape([-1, 3]), self.squareMicData[:, :, 7].ravel())

    def load_I9mic(self,fName):
        '''
        load mic file
        set voxelPos,voxelAcceptedEuler, voxelHitRatio,micEuler
        :param fNmame:
        %% Legacy File Format:
          %% Col 0-2 x, y, z
          %% Col 3   1 = triangle pointing up, 2 = triangle pointing down
          %% Col 4 generation number; triangle size = sidewidth /(2^generation number )
          %% Col 5 Phase - 1 = exist, 0 = not fitted
          %% Col 6-8 orientation
          %% Col 9  Confidence
        :return:
        '''

        # self.micData = np.loadtxt(fName,skiprows=skiprows)
        # self.micSideWith =
        # print(self.micData)
        # if self.micData.ndim==1:
        #     micData = self.micData[np.newaxis,:]
        # if self.micData.ndim==0:
        #     raise ValueError('number of dimension of mic file is wrong')
        # self.set_voxel_pos(self.micData[:,:3])
        with open(fName) as f:
            content = f.readlines()
        # print(content[1])
        # print(type(content[1]))
        sw = float(content[0])
        try:
            snp = np.array([[float(i) for i in s.split()] for s in content[1:]])
        except ValueError:
            print 'unknown deliminater'
        if snp.ndim<2:
            raise ValueError('snp dimension if not right, possible empty mic file or empty line in micfile')
        self.micSideWidth = sw
        self.micData = snp
        # set the center of triable to voxel position
        voxelpos = snp[:,:3].copy()
        voxelpos[:,0] = snp[:,0] + 0.5*sw/(2**snp[:,4])
        voxelpos[:,1] = snp[:,1] + 2*(1.5-snp[:,3]) * sw/(2**snp[:,4])/2/np.sqrt(3)
        self.set_voxel_pos(voxelpos)

    def save_mic(self,fName):
        '''
        save mic
        :param fName:
        :return:
        '''
        print('======= saved to mic file: {0} ========'.format(fName))
        np.savetxt(fName, self.micData, fmt=['%.6f'] * 2 + ['%d'] * 4 + ['%.6f'] * (self.micData.shape[1] - 6),
                   delimiter='\t', header=str(self.micSideWidth), comments='')

    def load_fz(self,fName):
        # load FZ.dat file
        # self.FZEuler: n_Orientation*3 array
        #test passed
        self.FZEuler = np.loadtxt(fName)
        # initialize orientation Matrices !!! implement on GPU later
        # self.FZMat = np.zeros([self.FZEuler.shape[0], 3, 3])
        if self.FZEuler.ndim == 1:
            print('wrong format of input orientation, should be nx3 numpy array')
        self.FZMat = RotRep.EulerZXZ2MatVectorized(self.FZEuler/ 180.0 * np.pi)
        # for i in range(self.FZEuler.shape[0]):
        #     self.FZMat[i, :, :] = RotRep.EulerZXZ2Mat(self.FZEuler[i, :] / 180.0 * np.pi).reshape(
        #         [3, 3])
        return self.FZEuler

    def load_exp_data(self,fInitials,digits):
        '''
        load experimental binary data self.expData[detIdx,rotIdx,j,k]
        these data are NOT transfered to GPU yet.
        :param fInitials: e.g./home/heliu/work/I9_test_data/Integrated/S18_z1_
        :param digits: number of digits in file name, usually 6,
        :return:
        '''
        lJ = []
        lK = []
        lRot = []
        lDet = []
        lIntensity = []
        lID = []
        for i in range(self.NDet):
            for j in range(self.NRot):
                print('loading det {0}, rotation {1}'.format(i,j))
                fName = fInitials+str(j).zfill(digits) + '.bin' + str(i)
                x,y,intensity,id = IntBin.ReadI9BinaryFiles(fName)
                lJ.append(x[:,np.newaxis])
                lK.append(y[:,np.newaxis])
                lDet.append(i*np.ones(x[:,np.newaxis].shape))
                lRot.append(j*np.ones(x[:,np.newaxis].shape))
                lIntensity.append(intensity[:,np.newaxis])
                lID.append(id)
        self.expData = np.concatenate([np.concatenate(lDet,axis=0),np.concatenate(lRot,axis=0),np.concatenate(lJ,axis=0),np.concatenate(lK,axis=0)],axis=1)
        print('exp data loaded, shape is: {0}.'.format(self.expData.shape))

    def cp_expdata_to_gpu_bakcup_20180206(self):
        # require have defiend self.NDet,self.NRot, and Detctor informations;
        #self.expData = np.array([[0,24,324,320],[0,0,0,1]]) # n_Peak*3,[detIndex,rotIndex,J,K] !!! be_careful this could go wrong is assuming wrong number of detectors
        #self.expData = np.array([[0,24,648,640],[0,172,285,631],[1,24,720,485],[1,172,207,478]]) #[detIndex,rotIndex,J,K]
        print('=============start of copy exp data to gpu ===========')
        if self.expData.shape[1]!=4:
            raise ValueError('expdata shape should be n_peaks*4')
        if np.max(self.expData[:,0])>self.NDet-1:
            raise ValueError('expData contains detector index out of bound')
        if np.max(self.expData[:,1])>self.NRot-1:
            raise  ValueError('expData contaisn rotation number out of bound')
        self.aiDetStartIdxH = [0] # index of Detctor start postition in self.acExpDetImages, e.g. 3 detectors with size 2048x2048, 180 rotations, self.aiDetStartIdx = [0,180*2048*2048,2*180*2048*2048]
        self.iExpDetImageSize = 0
        for i in range(self.NDet):
            self.iExpDetImageSize += self.NRot*self.detectors[i].NPixelJ*self.detectors[i].NPixelK
            if i<(self.NDet-1):
                self.aiDetStartIdxH.append(self.iExpDetImageSize)
        # check is detector size boyond the number int type could hold
        if self.iExpDetImageSize<0 or self.iExpDetImageSize>2147483647:
            raise ValueError("detector image size {0} is wrong, \n\
                             possible too large detector size\n\
                            currently use int type as detector pixel index\n\
                            future implementation use lognlong will solve this issure")

        self.aiDetStartIdxH = np.array(self.aiDetStartIdxH)
        self.acExpDetImages = gpuarray.zeros(self.iExpDetImageSize,np.int8)   # experimental image data on GPUlen=sigma_i(NDet*NRot*NPixelJ[i]*NPxielK[i])
        self.aiDetStartIdxD = gpuarray.to_gpu(self.aiDetStartIdxH.astype(np.int32))
        self.afDetInfoD = gpuarray.to_gpu(self.afDetInfoH.astype(np.float32))

        self.aiDetIndxD = gpuarray.to_gpu(self.expData[:, 0].ravel().astype(np.int32))
        self.aiRotND = gpuarray.to_gpu(self.expData[:, 1].ravel().astype(np.int32))
        self.aiJExpD = gpuarray.to_gpu(self.expData[:, 2].ravel().astype(np.int32))
        self.aiKExpD = gpuarray.to_gpu(self.expData[:, 3].ravel().astype(np.int32))
        self.iNPeak = np.int32(self.expData.shape[0])
        create_bin_expimages = mod.get_function("create_bin_expimages")
        create_bin_expimages(self.acExpDetImages, self.aiDetStartIdxD, self.afDetInfoD, np.int32(self.NDet), np.int32(self.NRot),
                             self.aiDetIndxD, self.aiRotND, self.aiJExpD, self.aiKExpD, self.iNPeak, block=(256,1,1),grid=(self.iNPeak//256+1,1))
        print('=============end of copy exp data to gpu ===========')
        # self.out_expdata = self.acExpDetImages.get()
        # for i in range(self.NDet):
        #     detImageSize = self.NRot*self.detectors[i].NPixelK*self.detectors[i].NPixelJ
        #     print(self.out_expdata[self.aiDetStartIdxH[i]:(self.aiDetStartIdxH[i]+detImageSize)].reshape([self.NRot,self.detectors[i].NPixelK,self.detectors[i].NPixelJ]))

    def sim_precheck(self):
        #check if inputs are correct
        if self.NDet!= len(self.detectors):
            raise ValueError('self.NDet does not match self.detectors')
        if  not np.any(self.oriMatToSim):
            raise  ValueError(' oriMatToSim not set ')

    def serial_recon_precheck(self):
        pass

    def serial_recon_multistage_precheck(self):
        if os.path.isfile(self.squareMicOutFile):
            input = raw_input('Warning: the out put file {0} have already exist, do you want to overwrite? \n \
                            Any button to continue, Ctr+C to quit'.format(self.squareMicData))
        if self.squareMicData is None:
            raise ValueError('have not initiate square mic yet')
    def run_sim(self):
        '''
        example usage:
            S = Reconstructor_GPU()
            S.load_mic('/home/heliu/Dropbox/pycuda/test_recon_one_grain_20180124.txt')
            S.oriMatToSim = RotRep.EulerZXZ2MatVectorized(S.micData[:,6:9])[0,:,:].reshape(-1,3,3)
            S.oriMatToSim = S.oriMatToSim.repeat(S.NVoxel,axis=0)
            print('rotmatrices: {0}'.format(S.oriMatToSim))
            S.run_sim()
            S.print_sim_results()
        :return:
        '''
        # timing tools:
        start = cuda.Event()
        end = cuda.Event()
        start.record()  # start timing
        # initialize Scattering Vectors
        self.sample.getRecipVec()
        self.sample.getGs(self.maxQ)

        # initialize orientation Matrices !!! implement on GPU later

        #initialize device parameters and outputs
        afOrientationMatD = gpuarray.to_gpu(self.oriMatToSim.astype(np.float32))
        afGD = gpuarray.to_gpu(self.sample.Gs.astype(np.float32))
        afVoxelPosD = gpuarray.to_gpu(self.voxelpos.astype(np.float32))
        afDetInfoD = gpuarray.to_gpu(self.afDetInfoH.astype(np.float32))
        if self.oriMatToSim.shape[0]%self.NVoxel !=0:
            raise ValueError('dimension of oriMatToSim should be integer number  of NVoxel')
        NOriPerVoxel = self.oriMatToSim.shape[0]/self.NVoxel
        self.NG = self.sample.Gs.shape[0]
        #output device parameters:
        aiJD = gpuarray.empty(self.NVoxel*NOriPerVoxel*self.NG*2*self.NDet,np.int32)
        aiKD = gpuarray.empty(self.NVoxel*NOriPerVoxel*self.NG*2*self.NDet,np.int32)
        afOmegaD= gpuarray.empty(self.NVoxel*NOriPerVoxel*self.NG*2*self.NDet,np.float32)
        abHitD = gpuarray.empty(self.NVoxel*NOriPerVoxel*self.NG*2*self.NDet,np.bool_)
        aiRotND = gpuarray.empty(self.NVoxel*NOriPerVoxel*self.NG*2*self.NDet, np.int32)
        sim_func = mod.get_function("simulation")


        # start of simulation
        print('============start of simulation ============= \n')
        start.record()  # start timing
        print('nvoxel: {0}, norientation:{1}\n'.format(self.NVoxel,NOriPerVoxel))
        self.sim_precheck()
        sim_func(aiJD, aiKD, afOmegaD, abHitD, aiRotND,\
                 np.int32(self.NVoxel), np.int32(NOriPerVoxel), np.int32(self.NG), np.int32(self.NDet), afOrientationMatD,afGD,\
                 afVoxelPosD,np.float32(self.energy),np.float32(self.etalimit), afDetInfoD,\
                 grid=(self.NVoxel,NOriPerVoxel), block=(self.NG,1,1))
        context.synchronize()
        end.record()
        self.aJH = aiJD.get()
        self.aKH = aiKD.get()
        self.aOmegaH = afOmegaD.get()
        self.bHitH = abHitD.get()
        self.aiRotNH = aiRotND.get()

        end.synchronize()
        print('============end of simulation================ \n')
        secs = start.time_till(end) * 1e-3
        print("SourceModule time {0} seconds.".format(secs))

        #self.print_sim_results()

    def single_voxel_recon_v20180205(self, voxelIdx, afFZMatD, NSearchOrien, NIteration=10, BoundStart=0.5):
        # This is the most robust version of single voxel recon
        # reconstruction of single voxel
        afVoxelPosD = gpuarray.to_gpu(self.voxelpos[voxelIdx, :].astype(np.float32))
        for i in range(NIteration):
            # print(i)
            # print('nvoxel: {0}, norientation:{1}'.format(1, NSearchOrien)
            # update rotation matrix to search
            if i == 0:
                rotMatSearchD = afFZMatD.copy()
            else:
                rotMatSearchD = self.gen_random_matrix(maxMatD, self.NSelect,
                                                       NSearchOrien // self.NSelect + 1, BoundStart * (0.7 ** i))
            afHitRatioH, aiPeakCntH = self.unit_run_hitratio(afVoxelPosD, rotMatSearchD, 1, NSearchOrien)
            maxHitratioIdx = np.argsort(afHitRatioH)[
                             :-(self.NSelect + 1):-1]  # from larges hit ratio to smaller
            maxMatIdx = 9 * maxHitratioIdx.ravel().repeat(9)  # self.NSelect*9
            for jj in range(1, 9):
                maxMatIdx[jj::9] = maxMatIdx[0::9] + jj
            maxHitratioIdxD = gpuarray.to_gpu(maxMatIdx.astype(np.int32))
            maxMatD = gpuarray.take(rotMatSearchD, maxHitratioIdxD)
            # print('max hitratio: {0},maxMat: {1}'.format(afHitRatioH[maxHitratioIdx[0]], maxMat[0, :, :]))
            #print('voxelIdx: {0}, max hitratio: {1}, peakcnt: {2}'.format(voxelIdx,afHitRatioH[maxHitratioIdx[0]],aiPeakCntH[maxHitratioIdx[0]]))
            del rotMatSearchD

        maxMat = maxMatD.get().reshape([-1, 3, 3])
        print('voxelIdx: {0}, max hitratio: {1}, peakcnt: {2},reconstructed euler angle {3}'.format(voxelIdx, afHitRatioH[maxHitratioIdx[0]],
                                                                      aiPeakCntH[maxHitratioIdx[0]],np.array(RotRep.Mat2EulerZXZ(maxMat[0, :, :])) / np.pi * 180))
        self.voxelAcceptedMat[voxelIdx, :, :] = RotRep.Orien2FZ(maxMat[0, :, :], 'Hexagonal')[0]
        self.voxelHitRatio[voxelIdx] = afHitRatioH[maxHitratioIdx[0]]
        del afVoxelPosD
    def single_voxel_recon_v20180206(self, voxelIdx, afFZMatD, NSearchOrien, NIteration=10, BoundStart=0.5):
        '''
        THis is a working version, no error so far as 20180130
        try to eliminate the number of memory allocation on GPU, but this seemed go wrong is previous attempts.
        :param voxelIdx:
        :param afFZMatD:
        :param NSearchOrien:
        :param NIteration:
        :param BoundStart:
        :return:
        '''
        # reconstruction of single voxel
        NBlock = 16    #Strange it may be, but this parameter will acturally affect reconstruction speed (25s to 31 seconds/100voxel)
        NVoxel = 1
        afVoxelPosD = gpuarray.to_gpu(self.voxelpos[voxelIdx, :].astype(np.float32))
        aiJD = cuda.mem_alloc(NVoxel * NSearchOrien * self.NG * 2 * self.NDet * np.int32(0).nbytes)
        aiKD = cuda.mem_alloc(NVoxel * NSearchOrien * self.NG * 2 * self.NDet * np.int32(0).nbytes)
        afOmegaD = cuda.mem_alloc(NVoxel * NSearchOrien * self.NG * 2 * self.NDet * np.float32(0).nbytes)
        abHitD = cuda.mem_alloc(NVoxel * NSearchOrien * self.NG * 2 * self.NDet * np.bool_(0).nbytes)
        aiRotND = cuda.mem_alloc(NVoxel * NSearchOrien * self.NG * 2 * self.NDet * np.int32(0).nbytes)
        afHitRatioD = cuda.mem_alloc(NVoxel * NSearchOrien * np.float32(0).nbytes)
        aiPeakCntD = cuda.mem_alloc(NVoxel * NSearchOrien * np.int32(0).nbytes)
        afHitRatioH = np.empty(NVoxel * NSearchOrien, np.float32)
        aiPeakCntH = np.empty(NVoxel * NSearchOrien, np.int32)
        for i in range(NIteration):
            # print(i)
            # print('nvoxel: {0}, norientation:{1}'.format(1, NSearchOrien)
            # update rotation matrix to search
            if i == 0:
                rotMatSearchD = afFZMatD.copy()
            else:
                rotMatSearchD = self.gen_random_matrix(maxMatD, self.NSelect,
                                                       NSearchOrien // self.NSelect + 1, BoundStart * (0.5 ** i))

            #afHitRatioH, aiPeakCntH = self.unit_run_hitratio(afVoxelPosD, rotMatSearchD, 1, NSearchOrien)
            # kernel calls
            #start = time.time()
            self.sim_func(aiJD, aiKD, afOmegaD, abHitD, aiRotND, \
                          np.int32(NVoxel), np.int32(NSearchOrien), np.int32(self.NG), np.int32(self.NDet),
                          rotMatSearchD, self.afGD,
                          afVoxelPosD, np.float32(self.energy), np.float32(self.etalimit), self.afDetInfoD,
                          grid=(NVoxel, NSearchOrien), block=(self.NG, 1, 1))

            # this is the most time cosuming part, 0.03s per iteration
            self.hitratio_func(np.int32(NVoxel), np.int32(NSearchOrien), np.int32(self.NG),
                               self.afDetInfoD, self.acExpDetImages, self.aiDetStartIdxD, np.int32(self.NDet),
                               np.int32(self.NRot),
                               aiJD, aiKD, aiRotND, abHitD,
                               afHitRatioD, aiPeakCntD,
                               block=(NBlock, 1, 1), grid=((NVoxel * NSearchOrien - 1) // NBlock + 1, 1))

            # print('finish sim')
            # memcpy_dtoh
            context.synchronize()

            cuda.memcpy_dtoh(afHitRatioH, afHitRatioD)
            cuda.memcpy_dtoh(aiPeakCntH, aiPeakCntD)
            #end = time.time()
            #print("SourceModule time {0} seconds.".format(end-start))
            maxHitratioIdx = np.argsort(afHitRatioH)[
                             :-(self.NSelect + 1):-1]  # from larges hit ratio to smaller
            maxMatIdx = 9 * maxHitratioIdx.ravel().repeat(9)  # self.NSelect*9
            for jj in range(1, 9):
                maxMatIdx[jj::9] = maxMatIdx[0::9] + jj
            maxHitratioIdxD = gpuarray.to_gpu(maxMatIdx.astype(np.int32))
            maxMatD = gpuarray.take(rotMatSearchD, maxHitratioIdxD)
            del rotMatSearchD
        aiJD.free()
        aiKD.free()
        afOmegaD.free()
        abHitD.free()
        aiRotND.free()
        afHitRatioD.free()
        aiPeakCntD.free()
        maxMat = maxMatD.get().reshape([-1, 3, 3])
        print('voxelIdx: {0}, max hitratio: {1}, peakcnt: {2},reconstructed euler angle {3}'.format(voxelIdx,
                                                                                                    afHitRatioH[
                                                                                                        maxHitratioIdx[
                                                                                                            0]],
                                                                                                    aiPeakCntH[
                                                                                                        maxHitratioIdx[
                                                                                                            0]],
                                                                                                    np.array(
                                                                                                        RotRep.Mat2EulerZXZ(
                                                                                                            maxMat[0, :,
                                                                                                            :])) / np.pi * 180))
        self.voxelAcceptedMat[voxelIdx, :, :] = RotRep.Orien2FZ(maxMat[0, :, :], 'Hexagonal')[0]
        self.voxelHitRatio[voxelIdx] = afHitRatioH[maxHitratioIdx[0]]
        del afVoxelPosD

    def single_voxel_recon_acc1(self, voxelIdx, afFZMatD, NSearchOrien, NIteration=10, BoundStart=0.5):
        '''
        # this version tries to use kernal that combines sim and hit ratio.
        THis is a working version, no error so far as 20180130
        try to eliminate the number of memory allocation on GPU, but this seemed go wrong is previous attempts.
        :param voxelIdx:
        :param afFZMatD:
        :param NSearchOrien:
        :param NIteration:
        :param BoundStart:
        :return:
        '''
        # reconstruction of single voxel
        NBlock = 16    #Strange it may be, but this parameter will acturally affect reconstruction speed (25s to 31 seconds/100voxel)
        NVoxel = 1
        afVoxelPosD = gpuarray.to_gpu(self.voxelpos[voxelIdx, :].astype(np.float32))
        aiJD = cuda.mem_alloc(NVoxel * NSearchOrien * self.NG * 2 * self.NDet * np.int32(0).nbytes)
        aiKD = cuda.mem_alloc(NVoxel * NSearchOrien * self.NG * 2 * self.NDet * np.int32(0).nbytes)
        afOmegaD = cuda.mem_alloc(NVoxel * NSearchOrien * self.NG * 2 * self.NDet * np.float32(0).nbytes)
        abHitD = cuda.mem_alloc(NVoxel * NSearchOrien * self.NG * 2 * self.NDet * np.bool_(0).nbytes)
        aiRotND = cuda.mem_alloc(NVoxel * NSearchOrien * self.NG * 2 * self.NDet * np.int32(0).nbytes)
        afHitRatioD = cuda.mem_alloc(NVoxel * NSearchOrien * np.float32(0).nbytes)
        aiPeakCntD = cuda.mem_alloc(NVoxel * NSearchOrien * np.int32(0).nbytes)
        afHitRatioH = np.empty(NVoxel * NSearchOrien, np.float32)
        aiPeakCntH = np.empty(NVoxel * NSearchOrien, np.int32)
        for i in range(NIteration):
            # print(i)
            # print('nvoxel: {0}, norientation:{1}'.format(1, NSearchOrien)
            # update rotation matrix to search
            if i == 0:
                rotMatSearchD = afFZMatD.copy()
            else:
                rotMatSearchD = self.gen_random_matrix(maxMatD, self.NSelect,
                                                       NSearchOrien // self.NSelect + 1, BoundStart * (0.7 ** i))

            #afHitRatioH, aiPeakCntH = self.unit_run_hitratio(afVoxelPosD, rotMatSearchD, 1, NSearchOrien)
            # kernel calls
            self.sim_hitratio_unit(aiJD, aiKD, afOmegaD, abHitD, aiRotND, \
                          np.int32(NVoxel), np.int32(NSearchOrien), np.int32(self.NG), np.int32(self.NDet),
                          rotMatSearchD, self.afGD,
                          afVoxelPosD, np.float32(self.energy), np.float32(self.etalimit), self.afDetInfoD,
                          self.acExpDetImages, self.aiDetStartIdxD,
                          afHitRatioD, aiPeakCntD,
                          grid=(NVoxel, NSearchOrien), block=(self.NG, 1, 1),shared=self.NG*4*(np.int32(0).nbytes))
            # memcpy_dtoh
            context.synchronize()
            #end = time.time()
            cuda.memcpy_dtoh(afHitRatioH, afHitRatioD)
            cuda.memcpy_dtoh(aiPeakCntH, aiPeakCntD)

            #print("SourceModule time {0} seconds.".format(end-start))
            maxHitratioIdx = np.argsort(afHitRatioH)[
                             :-(self.NSelect + 1):-1]  # from larges hit ratio to smaller
            maxMatIdx = 9 * maxHitratioIdx.ravel().repeat(9)  # self.NSelect*9
            for jj in range(1, 9):
                maxMatIdx[jj::9] = maxMatIdx[0::9] + jj
            maxHitratioIdxD = gpuarray.to_gpu(maxMatIdx.astype(np.int32))
            maxMatD = gpuarray.take(rotMatSearchD, maxHitratioIdxD)
            del rotMatSearchD
        aiJD.free()
        aiKD.free()
        afOmegaD.free()
        abHitD.free()
        aiRotND.free()
        afHitRatioD.free()
        aiPeakCntD.free()
        maxMat = maxMatD.get().reshape([-1, 3, 3])
        print('voxelIdx: {0}, max hitratio: {1}, peakcnt: {2},reconstructed euler angle {3}'.format(voxelIdx,
                                                                                                    afHitRatioH[
                                                                                                        maxHitratioIdx[
                                                                                                            0]],
                                                                                                    aiPeakCntH[
                                                                                                        maxHitratioIdx[
                                                                                                            0]],
                                                                                                    np.array(
                                                                                                        RotRep.Mat2EulerZXZ(
                                                                                                            maxMat[0, :,
                                                                                                            :])) / np.pi * 180))
        self.voxelAcceptedMat[voxelIdx, :, :] = RotRep.Orien2FZ(maxMat[0, :, :], 'Hexagonal')[0]
        self.voxelHitRatio[voxelIdx] = afHitRatioH[maxHitratioIdx[0]]
        del afVoxelPosD
    def expansion_unit_run(self, voxelIdx, afFZMatD):
        '''
        These is just a simple combination of single_voxel_recon and flood_fill
        :param voxelIdx:
        :param afFZMatD:
        :return:
        '''
        self.single_voxel_recon(voxelIdx, afFZMatD, self.searchBatchSize)
        if self.voxelHitRatio[voxelIdx] > self.floodFillStartThreshold:
            self.flood_fill(voxelIdx)
            self.NFloodFill += 1

    def unit_run_hitratio(self,afVoxelPosD, rotMatSearchD, NVoxel, NOrientation):
        '''
        These is the most basic building block, simulate peaks and calculate their hit ratio
        CAUTION: strange bug, if NVoxel*NOrientation is too small( < 200), memery access erro will occur.
        :param afVoxelPosD: NVoxel*3
        :param rotMatSearchD: NVoxel*NOrientation*9
        :param NVoxel:
        :param NOrientation:
        :return:
        '''
        # if not (isinstance(afVoxelPosD, pycuda.gpuarray.GPUArray) and isinstance(rotMatSearchD, pycuda.gpuarray.GPUArray)):
        #     raise TypeError('afVoxelPosD and rotMatSearchD should be gpuarray, not allocator or other.')
        # if NVoxel*NOrientation < 350:
        #     print(" number of input may be too small")
        # if NVoxel==0 or NOrientation==0:
        #     print('number of voxel {0} and orientation {1} is not in right form'.format(NVoxel,NOrientation))
        #     return 0,0
        aiJD = cuda.mem_alloc(NVoxel * NOrientation * self.NG * 2 * self.NDet * np.int32(0).nbytes)
        aiKD = cuda.mem_alloc(NVoxel * NOrientation * self.NG * 2 * self.NDet * np.int32(0).nbytes)
        afOmegaD = cuda.mem_alloc(NVoxel * NOrientation * self.NG * 2 * self.NDet * np.float32(0).nbytes)
        abHitD = cuda.mem_alloc(NVoxel * NOrientation * self.NG * 2 * self.NDet * np.bool_(0).nbytes)
        aiRotND = cuda.mem_alloc(NVoxel * NOrientation * self.NG * 2 * self.NDet * np.int32(0).nbytes)
        # kernel calls
        self.sim_func(aiJD, aiKD, afOmegaD, abHitD, aiRotND, \
                      np.int32(NVoxel), np.int32(NOrientation), np.int32(self.NG), np.int32(self.NDet),
                      rotMatSearchD,
                      afVoxelPosD, np.float32(self.energy), np.float32(self.etalimit), self.afDetInfoD,
                      texrefs=[self.tfG], grid=(NVoxel, NOrientation), block=(self.NG, 1, 1))
        afHitRatioD = cuda.mem_alloc(NVoxel * NOrientation * np.float32(0).nbytes)
        aiPeakCntD = cuda.mem_alloc(NVoxel * NOrientation * np.int32(0).nbytes)
        NBlock = 256
        self.hitratio_func(np.int32(NVoxel), np.int32(NOrientation), np.int32(self.NG),
                           self.afDetInfoD, np.int32(self.NDet),
                           np.int32(self.NRot),
                           aiJD, aiKD, aiRotND, abHitD,
                           afHitRatioD, aiPeakCntD,texrefs=[self.texref],
                           block=(NBlock, 1, 1), grid=((NVoxel * NOrientation - 1) // NBlock + 1, 1))
        # print('finish sim')
        # memcpy_dtoh
        context.synchronize()
        afHitRatioH = np.empty(NVoxel*NOrientation,np.float32)
        aiPeakCntH = np.empty(NVoxel*NOrientation, np.int32)
        cuda.memcpy_dtoh(afHitRatioH, afHitRatioD)
        cuda.memcpy_dtoh(aiPeakCntH, aiPeakCntD)
        aiJD.free()
        aiKD.free()
        afOmegaD.free()
        abHitD.free()
        aiRotND.free()
        afHitRatioD.free()
        aiPeakCntD.free()
        return afHitRatioH, aiPeakCntH

    def profile_recon_layer(self):
        '''
                ==================working version =========================
                Todo:
                    fix two reconstructed orientation:
                        simulate the two different reconstructed oreintation and compare peaks
                        compute their misoritentation
                serial reconstruct orientation in a layer, loaded in mic file
                example usage:
                R = Reconstructor_GPU()
                Not Implemented: Setup R experimental parameters
                R.searchBatchSize = 20000  # number of orientations to search per GPU call
                R.load_mic('/home/heliu/work/I9_test_data/FIT/DataFiles/Ti_SingleGrainFit1_.mic.LBFS')
                R.load_fz('/home/heliu/work/I9_test_data/FIT/DataFiles/HexFZ.dat')
                R.load_exp_data('/home/heliu/work/I9_test_data/Integrated/S18_z1_', 6)
                R.serial_recon_layer()
                :return:
                '''
        ############## reform for easy reading #############
        ############ added generate random in GPU #########3
        ############# search parameters ######################
        # try adding multiple stage search, first fz file, then generate random around max hitratio
        # self.load_mic('/home/heliu/work/I9_test_data/FIT/DataFiles/Ti_Fit1_.mic.LBFS')
        self.load_mic('test_recon_one_grain_20180124.txt')
        # self.load_mic('/home/heliu/work/I9_test_data/FIT/DataFiles/Ti_SingleGrainFit1_.mic.LBFS')
        # self.load_mic('/home/heliu/work/I9_test_data/FIT/test_recon.mic.LBFS')
        self.load_fz('/home/heliu/work/I9_test_data/FIT/DataFiles/HexFZ.dat')

        #self.load_exp_data('/home/heliu/work/I9_test_data/Integrated/S18_z1_', 6)
        #self.expData[:, 2:4] = self.expData[:, 2:4] / 4  # half the detctor size, to rescale real data
        self.expData = np.array([[1,2,3,4]])
        self.cp_expdata_to_gpu()

        # setup serial Reconstruction rotMatCandidate
        self.FZMatH = np.empty([self.searchBatchSize, 3, 3])
        if self.searchBatchSize > self.FZMat.shape[0]:
            self.FZMatH[:self.FZMat.shape[0], :, :] = self.FZMat
            self.FZMatH[self.FZMat.shape[0]:, :, :] = FZfile.generate_random_rot_mat(
                self.searchBatchSize - self.FZMat.shape[0])
        else:
            raise ValueError(" search batch size less than FZ file size, please increase search batch size")

        # initialize device parameters and outputs
        self.afGD = gpuarray.to_gpu(self.sample.Gs.astype(np.float32))
        self.afDetInfoD = gpuarray.to_gpu(self.afDetInfoH.astype(np.float32))
        afFZMatD = gpuarray.to_gpu(self.FZMatH.astype(np.float32))  # no need to modify during process

        # timing tools:
        start = cuda.Event()
        end = cuda.Event()

        print('==========start of reconstruction======== \n')
        start.record()  # start timing
        pr = cProfile.Profile()
        pr.enable()
        for voxelIdx in range(self.NVoxel):
            self.single_voxel_recon(voxelIdx, afFZMatD,self.searchBatchSize, NIteration=10)
        pr.disable()
        s = StringIO.StringIO()
        sortby = 'cumulative'
        ps = pstats.Stats(pr, stream=s).sort_stats(sortby)
        ps.print_stats()
        print s.getvalue()
        print('===========end of reconstruction========== \n')
        end.record()  # end timing
        end.synchronize()
        secs = start.time_till(end) * 1e-3
        print("SourceModule time {0} seconds.".format(secs))
        # save roconstruction result
        self.micData[:, 6:9] = RotRep.Mat2EulerZXZVectorized(self.voxelAcceptedMat) / np.pi * 180
        self.micData[:, 9] = self.voxelHitRatio
        self.save_mic('test_recon_one_grain_gpu_random_out.txt')
    def serial_recon_layer(self):
        '''
        ==================working version =========================
        Todo:
            fix two reconstructed orientation:
                simulate the two different reconstructed oreintation and compare peaks
                compute their misoritentation
        serial reconstruct orientation in a layer, loaded in mic file
        example usage:
        R = Reconstructor_GPU()
        Not Implemented: Setup R experimental parameters
        R.searchBatchSize = 20000  # number of orientations to search per GPU call
        R.load_mic('/home/heliu/work/I9_test_data/FIT/DataFiles/Ti_SingleGrainFit1_.mic.LBFS')
        R.load_fz('/home/heliu/work/I9_test_data/FIT/DataFiles/HexFZ.dat')
        R.load_exp_data('/home/heliu/work/I9_test_data/Integrated/S18_z1_', 6)
        R.serial_recon_layer()
        :return:
        '''
        ############## reform for easy reading #############
        ############ added generate random in GPU #########3
        ############# search parameters ######################
        # try adding multiple stage search, first fz file, then generate random around max hitratio

        #self.recon_prepare()

        # timing tools:
        start = cuda.Event()
        end = cuda.Event()

        print('==========start of reconstruction======== \n')
        start.record()  # start timing

        for voxelIdx in self.voxelIdxStage0:
            self.single_voxel_recon(voxelIdx, self.afFZMatD,self.searchBatchSize)
        print('===========end of reconstruction========== \n')
        end.record()  # end timing
        end.synchronize()
        secs = start.time_till(end) * 1e-3
        print("SourceModule time {0} seconds.".format(secs))
        # save roconstruction result
        self.squareMicData[:,:,3:6] = (RotRep.Mat2EulerZXZVectorized(self.voxelAcceptedMat)/np.pi*180).reshape([self.squareMicData.shape[0],self.squareMicData.shape[1],3])
        self.squareMicData[:,:,6] = self.voxelHitRatio.reshape([self.squareMicData.shape[0],self.squareMicData.shape[1]])
        self.save_square_mic(self.squareMicOutFile)
        #self.save_mic('test_recon_one_grain_gpu_random_out.txt')

    def fill_neighbour(self):
        '''
        NOT FINISHED, seems no need.
                check neighbour and fill the neighbour
                :return:
        '''
        print('====================== entering flood fill ===================================')
        # select voxels to conduct filling
        print('indexstage0 {0}'.format(len(self.voxelIdxStage0)))

        #lFloodFillIdx = list()
        lFloodFillIdx = list(
            np.where(np.logical_and(self.voxelHitRatio < self.floodFillSelectThreshold, self.voxelMask == 1))[0])

        if not lFloodFillIdx:
            return 0
        idxToAccept = []
        print(len(lFloodFillIdx))
        # try orientation to fill on all other voxels
        for i in range((len(lFloodFillIdx) - 1) // self.floodFillNumberVoxel + 1):  # make sure memory is enough

            idxTmp = lFloodFillIdx[i * self.floodFillNumberVoxel: (i + 1) * self.floodFillNumberVoxel]
            if len(idxTmp) == 0:
                print('no voxel to reconstruct')
                return 0
            elif len(idxTmp) < 350:
                idxTmp = idxTmp * (349 / len(idxTmp) + 1)
            print('i: {0}, idxTmp: {1}'.format(i, len(idxTmp)))
            afVoxelPosD = gpuarray.to_gpu(self.voxelpos[idxTmp, :].astype(np.float32))
            rotMatH = self.voxelAcceptedMat[self.voxelIdxStage0[0], :, :].reshape([-1, 3, 3]).repeat(len(idxTmp),
                                                                                                     axis=0).astype(
                np.float32)
            rotMatSearchD = gpuarray.to_gpu(rotMatH)
            afFloodHitRatioH, aiFloodPeakCntH = self.unit_run_hitratio(afVoxelPosD, rotMatSearchD, len(idxTmp), 1)

            idxToAccept.append(np.array(idxTmp)[afFloodHitRatioH > self.floodFillAccptThreshold])
            del afVoxelPosD
            del rotMatSearchD
        # print('idxToAccept: {0}'.format(idxToAccept))
        idxToAccept = np.concatenate(idxToAccept).ravel()
        # local optimize each voxel
        for i, idxTmp in enumerate(idxToAccept):
            # remove from stage 0
            try:
                self.voxelIdxStage0.remove(idxTmp)
            except ValueError:
                pass
            # do one time search:
            rotMatSearchD = self.gen_random_matrix(
                gpuarray.to_gpu(self.voxelAcceptedMat[self.voxelIdxStage0[0], :, :].astype(np.float32)),
                1, self.floodFillNumberAngle, self.floodFillRandomRange)
            self.single_voxel_recon(idxTmp, rotMatSearchD, self.floodFillNumberAngle,
                                    NIteration=self.floodFillNIteration, BoundStart=self.floodFillRandomRange)
            del rotMatSearchD
        # print('fill {0} voxels'.format(idxToAccept.shape))
        print('++++++++++++++++++ leaving flood fill +++++++++++++++++++++++')
        return 1
    def flood_fill(self, voxelIdx):
        '''
        This is acturally not a flood fill process, it tries to fill all the voxels with low hitratio
        flood fill all the voxel with confidence level lower than self.floodFillAccptThreshold
        :return:
        '''
        print('====================== entering flood fill ===================================')
        # select voxels for filling
        lFloodFillIdx = list(np.where(np.logical_and(self.voxelHitRatio<self.floodFillSelectThreshold, self.voxelMask==1))[0])
        if not lFloodFillIdx:
            return 0
        idxToAccept = []
        print(len(lFloodFillIdx))
        # try orientation to fill on all other voxels
        for i in range((len(lFloodFillIdx)-1)//self.floodFillNumberVoxel+1):     #make sure memory is enough

            idxTmp = lFloodFillIdx[i*self.floodFillNumberVoxel: (i+1)*self.floodFillNumberVoxel]
            if len(idxTmp)==0:
                print('no voxel to reconstruct')
                return 0
            elif len(idxTmp)<350:
                idxTmp = idxTmp * (349/len(idxTmp)+1)
            print('select group: {0}, number of voxel: {1}'.format(i,len(idxTmp)))
            afVoxelPosD = gpuarray.to_gpu(self.voxelpos[idxTmp,:].astype(np.float32))
            rotMatH = self.voxelAcceptedMat[voxelIdx, :, :].reshape([-1, 3, 3]).repeat(len(idxTmp),
                                                                                                     axis=0).astype(
                np.float32)
            rotMatSearchD = gpuarray.to_gpu(rotMatH)
            afFloodHitRatioH, aiFloodPeakCntH = self.unit_run_hitratio(afVoxelPosD,rotMatSearchD,len(idxTmp),1)

            idxToAccept.append(np.array(idxTmp)[afFloodHitRatioH>self.floodFillAccptThreshold])
            del afVoxelPosD
            del rotMatSearchD
        #print('idxToAccept: {0}'.format(idxToAccept))
        idxToAccept = np.concatenate(idxToAccept).ravel()
        # local optimize each voxel
        for i, idxTmp in enumerate(idxToAccept):
            # do one time search:
            rotMatSearchD = self.gen_random_matrix(gpuarray.to_gpu(self.voxelAcceptedMat[voxelIdx, :, :].astype(np.float32)),
                                                   1, self.floodFillNumberAngle, self.floodFillRandomRange)
            self.single_voxel_recon(idxTmp,rotMatSearchD,self.floodFillNumberAngle, NIteration=self.floodFillNIteration, BoundStart=self.floodFillRandomRange)
            # if self.voxelHitRatio[idxTmp]>self.floodFillSelectThreshold:
            try:
                self.voxelIdxStage0.remove(idxTmp)
            except ValueError:
                pass
            del rotMatSearchD
        print('this process fill {0} voxels'.format(idxToAccept.shape))
        print('voxels left: {0}'.format(len(self.voxelIdxStage0)))
        print('++++++++++++++++++ leaving flood fill +++++++++++++++++++++++')
        return 1
    def serial_recon_multi_stage(self):
        '''
                # add multiple stage in serial reconstruction:
        # Todo:
        #   done: add flood fill
        #   done: add post stage check, refill reconstructed orientations to each voxel.
        # Example Usage:
        :return:
        '''
        #self.recon_prepare()

        # timing tools:
        start = cuda.Event()
        end = cuda.Event()
        print('==========start of reconstruction======== \n')
        start.record()  # start timing
        self.NFloodFill = 0
        while self.voxelIdxStage0:
            # start of simulation
            voxelIdx = random.choice(self.voxelIdxStage0)
            self.single_voxel_recon(voxelIdx, self.afFZMatD,self.searchBatchSize)
            if self.voxelHitRatio[voxelIdx] > self.floodFillStartThreshold:
                self.flood_fill(voxelIdx)
                self.NFloodFill += 1
            try:
                self.voxelIdxStage0.remove(voxelIdx)
            except ValueError:
                pass
        print('number of flood fills: {0}'.format(self.NFloodFill))
        self.post_process()
        print('===========end of reconstruction========== \n')
        end.record()  # end timing
        end.synchronize()
        secs = start.time_till(end) * 1e-3
        print("SourceModule time {0} seconds.".format(secs))
        # save roconstruction result
        self.squareMicData[:,:,3:6] = (RotRep.Mat2EulerZXZVectorized(self.voxelAcceptedMat)/np.pi*180).reshape([self.squareMicData.shape[0],self.squareMicData.shape[1],3])
        self.squareMicData[:,:,6] = self.voxelHitRatio.reshape([self.squareMicData.shape[0],self.squareMicData.shape[1]])
        self.save_square_mic(self.squareMicOutFile)
        #self.save_mic('Ti7_S18_whole_layer_GPU_output.mic')
    def serial_recon_expansion_mode(self,startIdx):
        '''
        This is actually a flood fill process.
        In this mode, it will search the orientation at start point and gradually reachout to the boundary,
        So that it can save the time wasted on boundaries. ~ 10%~30% of total reconstruction time
        :return:
        '''
        #self.recon_prepare()

        # timing tools:
        start = cuda.Event()
        end = cuda.Event()
        print('==========start of reconstruction======== \n')
        start.record()  # start timing
        self.NFloodFill = 0
        NX = self.squareMicData.shape[0]
        NY = self.squareMicData.shape[1]
        visited = np.zeros(NX * NY)
        if self.voxelHitRatio[startIdx] == 0:
            self.expansion_unit_run(startIdx, self.afFZMatD)
        if self.voxelHitRatio[startIdx] < self.expansionStopHitRatio:
            raise ValueError(' this start point fail to initialize expansion, choose another starting point.')
        q = [startIdx]
        visited[startIdx] = 1
        while q:
            n = q.pop(0)
            for x in [min(n + 1, n-n%NY + NY-1), max(n-n%NY, n - 1), min(n//NY+1, NX-1)*NY + n%NY, max(0,n//NY-1)*NY + n%NY]:
                if self.voxelHitRatio[x] == 0:
                    self.expansion_unit_run(x, self.afFZMatD)
                if self.voxelHitRatio[x] > self.expansionStopHitRatio and visited[x] == 0:
                    q.append(x)
                    visited[x] = 1
        print('number of flood fills: {0}'.format(self.NFloodFill))
        self.post_process()
        print('===========end of reconstruction========== \n')
        end.record()  # end timing
        end.synchronize()
        secs = start.time_till(end) * 1e-3
        print("SourceModule time {0} seconds.".format(secs))
        # save roconstruction result
        self.squareMicData[:,:,3:6] = (RotRep.Mat2EulerZXZVectorized(self.voxelAcceptedMat)/np.pi*180).reshape([self.squareMicData.shape[0],self.squareMicData.shape[1],3])
        self.squareMicData[:,:,6] = self.voxelHitRatio.reshape([self.squareMicData.shape[0],self.squareMicData.shape[1]])
        self.save_square_mic(self.squareMicOutFile)
        #self.save_mic('Ti7_S18_whole_layer_GPU_output.mic')
    def print_sim_results(self):
        # print(self.aJH)
        # print(self.aKH)
        # print(self.aiRotNH)
        # print(self.aOmegaH)
        # print(self.bHitH)
        NOriPerVoxel = (self.oriMatToSim.shape[0] / self.NVoxel)
        for i,hit in enumerate(self.bHitH):
            if hit:
                print('VoxelIdx:{5}, Detector: {0}, J: {1}, K: {2},,RotN:{3}, Omega: {4}'.format(i%self.NDet, self.aJH[i],self.aKH[i],self.aiRotNH[i], self.aOmegaH[i],i//(NOriPerVoxel*self.NG*2*self.NDet)))
    def gen_random_matrix(self, matInD, NMatIn, NNeighbour, bound):
        '''
        generate orientations around cetrain rotation matrix
        :param matInD: gpuarray
        :param NMatIn: number of input mat
        :param NNeighbour:
        :param  bound, rangdom angle range.
        :return:
        '''
        # if NMatIn<=0 or NNeighbour<=0 or bound<=0:
        #     raise ValueError('number of matin {0} or nneighbour {1} or bound {2} is not right')
        #if isinstance(matInD,pycuda.gpuarray.GPUArray) or isinstance(matInD, pycuda._driver.DeviceAllocation):
        eulerD = gpuarray.empty(NMatIn*3, np.float32)
        matOutD = gpuarray.empty(NMatIn*NNeighbour*9, np.float32)
        NBlock = 128

        self.mat_to_euler_ZXZ(matInD, eulerD, np.int32(NMatIn), block=(NBlock, 1, 1), grid=((NMatIn-1) // NBlock + 1, 1))
        afRandD = self.randomGenerator.gen_uniform(NNeighbour * NMatIn * 3, np.float32)
        self.rand_mat_neighb_from_euler(eulerD, matOutD, afRandD, np.float32(bound), grid=(NNeighbour, 1), block=(NMatIn, 1, 1))
        return matOutD

    def cp_expdata_to_gpu(self):
        # try to use texture memory, assuming all detector have same size.
        # require have defiend self.NDet,self.NRot, and Detctor informations;
        #self.expData = np.array([[0,24,324,320],[0,0,0,1]]) # n_Peak*3,[detIndex,rotIndex,J,K] !!! be_careful this could go wrong is assuming wrong number of detectors
        #self.expData = np.array([[0,24,648,640],[0,172,285,631],[1,24,720,485],[1,172,207,478]]) #[detIndex,rotIndex,J,K]
        print('=============start of copy exp data to gpu ===========')
        if self.detectors[0].NPixelJ!=self.detectors[1].NPixelJ or self.detectors[0].NPixelK!=self.detectors[1].NPixelK:
            raise ValueError(' This version requare all detector have same dimension')
        if self.expData.shape[1]!=4:
            raise ValueError('expdata shape should be n_peaks*4')
        if np.max(self.expData[:,0])>self.NDet-1:
            raise ValueError('expData contains detector index out of bound')
        if np.max(self.expData[:,1])>self.NRot-1:
            raise  ValueError('expData contaisn rotation number out of bound')
        self.aiDetStartIdxH = [0] # index of Detctor start postition in self.acExpDetImages, e.g. 3 detectors with size 2048x2048, 180 rotations, self.aiDetStartIdx = [0,180*2048*2048,2*180*2048*2048]
        self.iExpDetImageSize = 0
        for i in range(self.NDet):
            self.iExpDetImageSize += self.NRot*self.detectors[i].NPixelJ*self.detectors[i].NPixelK
            if i<(self.NDet-1):
                self.aiDetStartIdxH.append(self.iExpDetImageSize)
        # check is detector size boyond the number int type could hold
        if self.iExpDetImageSize<0 or self.iExpDetImageSize>2147483647:
            raise ValueError("detector image size {0} is wrong, \n\
                             possible too large detector size\n\
                            currently use int type as detector pixel index\n\
                            future implementation use lognlong will solve this issure")

        self.aiDetStartIdxH = np.array(self.aiDetStartIdxH)
        self.acExpDetImages = gpuarray.zeros([self.NDet*self.NRot,self.detectors[0].NPixelK,self.detectors[0].NPixelJ],np.int8)   # experimental image data on GPUlen=sigma_i(NDet*NRot*NPixelJ[i]*NPxielK[i])
        self.aiDetStartIdxD = gpuarray.to_gpu(self.aiDetStartIdxH.astype(np.int32))
        self.afDetInfoD = gpuarray.to_gpu(self.afDetInfoH.astype(np.float32))

        self.aiDetIndxD = gpuarray.to_gpu(self.expData[:, 0].ravel().astype(np.int32))
        self.aiRotND = gpuarray.to_gpu(self.expData[:, 1].ravel().astype(np.int32))
        self.aiJExpD = gpuarray.to_gpu(self.expData[:, 2].ravel().astype(np.int32))
        self.aiKExpD = gpuarray.to_gpu(self.expData[:, 3].ravel().astype(np.int32))
        self.iNPeak = np.int32(self.expData.shape[0])
        create_bin_expimages = mod.get_function("create_bin_expimages")
        create_bin_expimages(self.acExpDetImages, self.aiDetStartIdxD, self.afDetInfoD, np.int32(self.NDet), np.int32(self.NRot),
                             self.aiDetIndxD, self.aiRotND, self.aiJExpD, self.aiKExpD, self.iNPeak, block=(256,1,1),grid=(self.iNPeak//256+1,1))
        # create texture memory
        self.texref = mod.get_texref("tcExpData")
        print('start of creating texture memory')
        self.texref.set_array(cuda.gpuarray_to_array(self.acExpDetImages,order='C'))
        self.texref.set_flags(cuda.TRSA_OVERRIDE_FORMAT)
        del self.acExpDetImages
        print('end of creating texture memory')
        #del self.acExpDetImages
        print('=============end of copy exp data to gpu ===========')
    def single_voxel_recon(self, voxelIdx, afFZMatD, NSearchOrien, NIteration=10, BoundStart=0.5):
        '''
        This version tries to use texture memory
        THis is a working version, no error so far as 20180130
        try to eliminate the number of memory allocation on GPU, but this seemed go wrong is previous attempts.
        :param voxelIdx:
        :param afFZMatD:
        :param NSearchOrien:
        :param NIteration:
        :param BoundStart:
        :return:
        '''
        # reconstruction of single voxel
        NBlock = 16    #Strange it may be, but this parameter will acturally affect reconstruction speed (25s to 31 seconds/100voxel)
        NVoxel = 1
        afVoxelPosD = gpuarray.to_gpu(self.voxelpos[voxelIdx, :].astype(np.float32))
        aiJD = cuda.mem_alloc(NVoxel * NSearchOrien * self.NG * 2 * self.NDet * np.int32(0).nbytes)
        aiKD = cuda.mem_alloc(NVoxel * NSearchOrien * self.NG * 2 * self.NDet * np.int32(0).nbytes)
        afOmegaD = cuda.mem_alloc(NVoxel * NSearchOrien * self.NG * 2 * self.NDet * np.float32(0).nbytes)
        abHitD = cuda.mem_alloc(NVoxel * NSearchOrien * self.NG * 2 * self.NDet * np.bool_(0).nbytes)
        aiRotND = cuda.mem_alloc(NVoxel * NSearchOrien * self.NG * 2 * self.NDet * np.int32(0).nbytes)
        afHitRatioD = cuda.mem_alloc(NVoxel * NSearchOrien * np.float32(0).nbytes)
        aiPeakCntD = cuda.mem_alloc(NVoxel * NSearchOrien * np.int32(0).nbytes)
        #afHitRatioH = np.random.randint(0,100,NVoxel * NSearchOrien)
        #aiPeakCntH = np.random.randint(0,100,NVoxel * NSearchOrien)
        afHitRatioH = np.empty(NVoxel * NSearchOrien, np.float32)
        aiPeakCntH = np.empty(NVoxel * NSearchOrien, np.int32)
        for i in range(NIteration):
            # print(i)
            # print('nvoxel: {0}, norientation:{1}'.format(1, NSearchOrien)
            # update rotation matrix to search
            if i == 0:
                rotMatSearchD = afFZMatD.copy()
            else:
                rotMatSearchD = self.gen_random_matrix(maxMatD, self.NSelect,
                                                       NSearchOrien // self.NSelect + 1, BoundStart * (0.5 ** i))

            #afHitRatioH, aiPeakCntH = self.unit_run_hitratio(afVoxelPosD, rotMatSearchD, 1, NSearchOrien)
            # kernel calls
            #start = time.time()
            self.sim_func(aiJD, aiKD, afOmegaD, abHitD, aiRotND, \
                          np.int32(NVoxel), np.int32(NSearchOrien), np.int32(self.NG), np.int32(self.NDet),
                          rotMatSearchD,
                          afVoxelPosD, np.float32(self.energy), np.float32(self.etalimit), self.afDetInfoD,
                          texrefs=[self.tfG], grid=(NVoxel, NSearchOrien), block=(self.NG, 1, 1))

            # this is the most time cosuming part, 0.03s per iteration
            self.hitratio_func(np.int32(NVoxel), np.int32(NSearchOrien), np.int32(self.NG),
                               self.afDetInfoD, np.int32(self.NDet),
                               np.int32(self.NRot),
                               aiJD, aiKD, aiRotND, abHitD,
                               afHitRatioD, aiPeakCntD,texrefs=[self.texref],
                               block=(NBlock, 1, 1), grid=((NVoxel * NSearchOrien - 1) // NBlock + 1, 1))

            # print('finish sim')
            # memcpy_dtoh
            context.synchronize()

            cuda.memcpy_dtoh(afHitRatioH, afHitRatioD)
            cuda.memcpy_dtoh(aiPeakCntH, aiPeakCntD)
            #end = time.time()
            #print("SourceModule time {0} seconds.".format(end-start))
            maxHitratioIdx = np.argsort(afHitRatioH)[
                             :-(self.NSelect + 1):-1]  # from larges hit ratio to smaller
            maxMatIdx = 9 * maxHitratioIdx.ravel().repeat(9)  # self.NSelect*9
            for jj in range(1, 9):
                maxMatIdx[jj::9] = maxMatIdx[0::9] + jj
            maxHitratioIdxD = gpuarray.to_gpu(maxMatIdx.astype(np.int32))
            maxMatD = gpuarray.take(rotMatSearchD, maxHitratioIdxD)
            del rotMatSearchD
        aiJD.free()
        aiKD.free()
        afOmegaD.free()
        abHitD.free()
        aiRotND.free()
        afHitRatioD.free()
        aiPeakCntD.free()
        maxMat = maxMatD.get().reshape([-1, 3, 3])
        sys.stdout.write('voxelIdx: {0}, max hitratio: {1}, peakcnt: {2},reconstructed euler angle {3} \r'.format(voxelIdx,
                                                                                                    afHitRatioH[
                                                                                                        maxHitratioIdx[
                                                                                                            0]],
                                                                                                    aiPeakCntH[
                                                                                                        maxHitratioIdx[
                                                                                                            0]],
                                                                                                    np.array(
                                                                                                        RotRep.Mat2EulerZXZ(
                                                                                                            maxMat[0, :,
                                                                                                            :])) / np.pi * 180))
        sys.stdout.flush()
        self.voxelAcceptedMat[voxelIdx, :, :] = RotRep.Orien2FZ(maxMat[0, :, :], 'Hexagonal')[0]
        self.voxelHitRatio[voxelIdx] = afHitRatioH[maxHitratioIdx[0]]
        del afVoxelPosD

    def single_voxel_recon_acc3(self, voxelIdx, afFZMatD, NSearchOrien, NIteration=10, BoundStart=0.5):
        '''
        failed!
        combine shared memory and texture memory
        # this version tries to use kernal that combines sim and hit ratio.
        THis is a working version, no error so far as 20180130
        try to eliminate the number of memory allocation on GPU, but this seemed go wrong is previous attempts.
        :param voxelIdx:
        :param afFZMatD:
        :param NSearchOrien:
        :param NIteration:
        :param BoundStart:
        :return:
        '''
        # reconstruction of single voxel
        NBlock = 16    #Strange it may be, but this parameter will acturally affect reconstruction speed (25s to 31 seconds/100voxel)
        NVoxel = 1
        afVoxelPosD = gpuarray.to_gpu(self.voxelpos[voxelIdx, :].astype(np.float32))
        afHitRatioD = cuda.mem_alloc(NVoxel * NSearchOrien * np.float32(0).nbytes)
        aiPeakCntD = cuda.mem_alloc(NVoxel * NSearchOrien * np.int32(0).nbytes)
        afHitRatioH = np.empty(NVoxel * NSearchOrien, np.float32)
        aiPeakCntH = np.empty(NVoxel * NSearchOrien, np.int32)
        for i in range(NIteration):
            # print(i)
            # print('nvoxel: {0}, norientation:{1}'.format(1, NSearchOrien)
            # update rotation matrix to search
            if i == 0:
                rotMatSearchD = afFZMatD.copy()
            else:
                rotMatSearchD = self.gen_random_matrix(maxMatD, self.NSelect,
                                                       NSearchOrien // self.NSelect + 1, BoundStart * (0.7 ** i))

            #afHitRatioH, aiPeakCntH = self.unit_run_hitratio(afVoxelPosD, rotMatSearchD, 1, NSearchOrien)
            # kernel calls
            self.sim_hitratio_unit(np.int32(NVoxel), np.int32(NSearchOrien), np.int32(self.NG), np.int32(self.NDet),
                          rotMatSearchD, self.afGD,
                          afVoxelPosD, np.float32(self.energy), np.float32(self.etalimit), self.afDetInfoD,
                          afHitRatioD, aiPeakCntD,texrefs=[self.texref],
                          grid=(NVoxel, NSearchOrien), block=(self.NG, 1, 1),shared=self.NG*8*self.NDet*(np.int32(0).nbytes))
            # memcpy_dtoh
            context.synchronize()
            #end = time.time()
            cuda.memcpy_dtoh(afHitRatioH, afHitRatioD)
            cuda.memcpy_dtoh(aiPeakCntH, aiPeakCntD)

            #print("SourceModule time {0} seconds.".format(end-start))
            maxHitratioIdx = np.argsort(afHitRatioH)[
                             :-(self.NSelect + 1):-1]  # from larges hit ratio to smaller
            maxMatIdx = 9 * maxHitratioIdx.ravel().repeat(9)  # self.NSelect*9
            for jj in range(1, 9):
                maxMatIdx[jj::9] = maxMatIdx[0::9] + jj
            maxHitratioIdxD = gpuarray.to_gpu(maxMatIdx.astype(np.int32))
            maxMatD = gpuarray.take(rotMatSearchD, maxHitratioIdxD)
            del rotMatSearchD
        afHitRatioD.free()
        aiPeakCntD.free()
        maxMat = maxMatD.get().reshape([-1, 3, 3])
        print('voxelIdx: {0}, max hitratio: {1}, peakcnt: {2},reconstructed euler angle {3}'.format(voxelIdx,
                                                                                                    afHitRatioH[
                                                                                                        maxHitratioIdx[
                                                                                                            0]],
                                                                                                    aiPeakCntH[
                                                                                                        maxHitratioIdx[
                                                                                                            0]],
                                                                                                    np.array(
                                                                                                        RotRep.Mat2EulerZXZ(
                                                                                                            maxMat[0, :,
                                                                                                            :])) / np.pi * 180))
        self.voxelAcceptedMat[voxelIdx, :, :] = RotRep.Orien2FZ(maxMat[0, :, :], 'Hexagonal')[0]
        self.voxelHitRatio[voxelIdx] = afHitRatioH[maxHitratioIdx[0]]
        del afVoxelPosD

############## test section ###############
def test_load_fz():
    S = Reconstructor_GPU()
    S.load_fz('/home/heliu/work/I9_test_data/FIT/DataFiles/MyFZ.dat')
    print(S.FZEuler)
    print((S.FZEuler.shape))
def test_load_expdata():
    S = Reconstructor_GPU()
    S.NDet = 2
    S.NRot = 2
    S.load_exp_data('/home/heliu/work/I9_test_data/Integrated/S18_z1_',6)
def calculate_misoren_euler_zxz(euler0,euler1):
    rotMat0 = RotRep.EulerZXZ2Mat(euler0 / 180.0 * np.pi)
    rotMat1 = RotRep.EulerZXZ2Mat(euler1 / 180.0 * np.pi)
    return RotRep.Misorien2FZ1(rotMat0,rotMat1,symtype='Hexagonal')
def test_floodfill():
    S = Reconstructor_GPU()
    S.load_mic('/home/heliu/Dropbox/pycuda/test_recon_one_grain_20180124.txt')
    S.flood_fill()
def test_square_mic():
    s = Reconstructor_GPU()
    s.create_square_mic()
    print(s.squareMicData)
    print(s.voxelpos)
    print(s.voxelMask)
def test_gpuarray_take():
    a = gpuarray.arange(0,100,1,dtype=np.int32)
    indices = gpuarray.to_gpu(np.array([1,2,4,5]).astype(np.int32))
    b = gpuarray.take(a,indices)
    print(b.get())
    del indices
    indices = gpuarray.to_gpu(np.array([7, 8, 23, 5]).astype(np.int32))
    b = gpuarray.take(a, indices)
    print(b.get())
def test_mat2eulerzxz():
    NEulerIn = 6
    euler = np.array([[111.5003, 80.7666, 266.397],[1.5003, 80.7666, 266.397]]).repeat(NEulerIn/2, axis=0) / 180 * np.pi
    print(euler)
    matIn = RotRep.EulerZXZ2MatVectorized(euler)
    matInD = gpuarray.to_gpu(matIn.astype(np.float32))
    eulerOutD = gpuarray.empty(NEulerIn*3,np.float32)
    func = mod.get_function("mat_to_euler_ZXZ")
    NBlock = 128
    func(matInD,eulerOutD, np.int32(NEulerIn),block=(NBlock,1,1), grid = (NEulerIn//NBlock+1, 1))
    eulerOutH = eulerOutD.get().reshape([-1,3])
    print(eulerOutH)
def test_rand_amt_neighb():
    NEulerIn = 2
    NEighbour = 2
    bound = 0.1
    euler = np.array([[89.5003, 80.7666, 266.397]]).repeat(NEulerIn, axis=0)/180*np.pi
    matIn = RotRep.EulerZXZ2MatVectorized(euler).repeat(NEighbour,axis=0)
    matInD = gpuarray.to_gpu(matIn.astype(np.float32))
    S = Reconstructor_GPU()
    matOutD = S.gen_random_matrix(matInD,NEulerIn,NEighbour,0.01)
    # print(matIn.shape)
    # eulerD = gpuarray.to_gpu(euler.astype(np.float32))
    # matOutD = gpuarray.empty(NEighbour*NEulerIn*9,np.float32)
    # g = MRG32k3aRandomNumberGenerator()
    # afRandD = g.gen_uniform(NEighbour*NEulerIn*3, np.float32)
    # func = mod.get_function("rand_mat_neighb_from_euler")
    # func(eulerD, matOutD, afRandD, np.float32(bound),grid = (NEighbour,1),block=(NEulerIn,1,1))
    matH = matOutD.get().reshape([-1,3,3])
    print(matH.reshape([-1,3,3]))
    print(RotRep.Mat2EulerZXZVectorized(matH)/np.pi*180)
    for i in range(matH.shape[0]):
        print(RotRep.Misorien2FZ1(matIn[i,:,:], matH[i,:,:], 'Hexagonal'))
def test_random():
    N = 100
    g = MRG32k3aRandomNumberGenerator()
    rand = g.gen_uniform(N, np.float32)
    disp_func = mod.get_function("display_rand")
    disp_func(rand, np.int32(N), block=(N,1,1))
def test_eulerzxz2mat():
    N = 10000
    euler = np.array([[89.5003, 80.7666, 266.397]]).repeat(N,axis=0)
    eulerD = gpuarray.to_gpu(euler.astype(np.float32))
    matD = gpuarray.empty(N*9, np.float32 )
    gpu_func = mod.get_function("euler_zxz_to_mat")
    gpu_func(eulerD,matD,np.int32(N),block=(N,1,1))
    print(matD.get().reshape([-1,3,3]))
    print(RotRep.EulerZXZ2MatVectorized(euler))
def test_post_process():
    S = Reconstructor_GPU()
    S.load_square_mic('SquareMicTest1.npy')
    S.voxelAcceptedMat = RotRep.EulerZXZ2MatVectorized(S.squareMicData[:,:,3:6].reshape([-1,3])/180.0*np.pi)
    S.voxelHitRatio = S.squareMicData[:,:,6].ravel()
    S.post_process()
def squareMicMIsOrienMap():
    squareMic0 = np.load('SearchBatchSize_13000_100x100_0.01_run_1.npy')
    squareMic1 = np.load('SearchBatchSize_13000_100x100_0.01.npy')
    m0 = RotRep.EulerZXZ2MatVectorized(squareMic0[:,:,3:6]/180.0*np.pi)
    m1 = RotRep.EulerZXZ2MatVectorized(squareMic1[:, :, 3:6] / 180.0 * np.pi)
    symMat = RotRep.GetSymRotMat('Hexagonal')
    misOrien = misorien(m0,m1,symMat).reshape([squareMic0.shape[0],squareMic0.shape[1]])
    np.save('misOrien_SearchBatchSize_13000_100x100_0.01_differentRun.npy',misOrien)
def profile_gen_random_gpu():
    '''
    10 time generate 20000 (total200000) random orientation takes 0.01s
    :return:
    '''
    S = Reconstructor_GPU()
    euler = np.array([89.5003, 80.7666, 266.397]) / 180 * np.pi
    matInD = gpuarray.to_gpu(RotRep.EulerZXZ2Mat(euler))
    start = cuda.Event()
    end = cuda.Event()
    start.record()
    for i in range(10):
        matInD = gpuarray.to_gpu(RotRep.EulerZXZ2Mat(euler))
        matOutD = S.gen_random_matrix(matInD,1,20000,0.01)
        del matInD
        del matOutD
    end.record()
    end.synchronize()
    secs = start.time_till(end) * 1e-3
    print("SourceModule time {0} seconds.".format(secs))
class SquareMic():
    def __init__(self,squareMicData, symtype='Hexagonal'):
        self.squareMicData = squareMicData
        self.NVoxelX = self.squareMicData.shape[0]
        self.NVoxelY = self.squareMicData.shape[1]
        self.symMat = RotRep.GetSymRotMat(symtype)
    def get_misorien_map(self,m0):
        '''
        map the misorienation map
        e.g. a 100x100 square voxel will give 99x99 misorientations if axis=0,
        but it will still return 100x100, filling 0 to the last row/column
        the misorientatino on that voxel is the max misorienta to its right or up side voxel
        :param axis: 0 for x direction, 1 for y direction.
        :return:
        '''
        m0 = m0.reshape((self.NVoxelX,self.NVoxelY,9))
        if m0.ndim<3:
            raise ValueError('input should be [nvoxelx,nvoxely,9] matrix')
        NVoxelX = m0.shape[0]
        NVoxelY = m0.shape[1]
        #m0 = self.voxelAcceptedMat.reshape([NVoxelX, NVoxelY, 9])
        m1 = np.empty([NVoxelX, NVoxelY, 9])
        # x direction misorientatoin
        m1[:-1,:,:] = m0[1:,:,:]
        m1[-1,:,:] = m0[-1,:,:]
        misorienX = misorien(m0, m1, self.symMat)
        # y direction misorientation
        m1[:,:-1,:] = m0[:,1:,:]
        m1[:,-1,:] = m0[:,-1,:]
        misorienY = misorien(m0, m1, self.symMat)
        self.misOrien = np.maximum(misorienX, misorienY).reshape([NVoxelX, NVoxelY])
        return self.misOrien
    def save_misOrienMap(self,fName):
        m0 = RotRep.EulerZXZ2MatVectorized(self.squareMicData[:,:,3:6]/180.0*np.pi)
        self.get_misorien_map(m0)
        np.save(fName, self.misOrien)

def grain_boundary():
    sMic0 = SquareMic(np.load('SearchBatchSize_13000_1000x1000_0.001.npy'))
    sMic1 = SquareMic(np.load('SearchBatchSize_13000_1000x1000_0.001_repeatrun_1.npy'))
    sMic0.save_misOrienMap('SearchBatchSize_13000_1000x1000_0.001_misOrienMap.npy')
    sMic1.save_misOrienMap('SearchBatchSize_13000_1000x1000_0.001_repeatrun_1_misOrienMap.npy')
def misorien_vs_hitratio():
    pass

############ example usages ###################
def test_tex_mem():
    S = Reconstructor_GPU()
    S.FZFile = '/home/heliu/work/I9_test_data/FIT/DataFiles/HexFZ.dat'
    S.expDataInitial = '/home/heliu/work/I9_test_data/Integrated/S18_z1_'
    S.expdataNDigit = 6
    S.create_square_mic([10,10],voxelsize=0.01)
    S.squareMicOutFile = 'SearchBatchSize_13000_10x10_0.01_tex_mem_run0.npy'
    S.searchBatchSize = 13000
    S.recon_prepare()
    S.serial_recon_layer()
    #S.serial_recon_expansion_mode(S.squareMicData.shape[0]*S.squareMicData.shape[1]/2 + S.squareMicData.shape[1]/2)
def recon_example():
    '''
    This is an example of how to use Reconstructor_GPU
    :return:
    '''
    S = Reconstructor_GPU()
    S.FZFile = '/home/heliu/work/I9_test_data/FIT/DataFiles/HexFZ.dat'
    S.expDataInitial = '/home/heliu/work/I9_test_data/Integrated/S18_z1_'
    S.expdataNDigit = 6
    S.create_square_mic([500,500],voxelsize=0.002)
    S.squareMicOutFile = 'SearchBatchSize_13000_500x500_0.002_tex_mem_run0.npy'
    S.searchBatchSize = 13000
    S.recon_prepare()
    #S.serial_recon_layer()
    S.serial_recon_multi_stage()
    #S.serial_recon_expansion_mode(S.squareMicData.shape[0]*S.squareMicData.shape[1]/2 + S.squareMicData.shape[1]/2)
def recon_aws():
    S = Reconstructor_GPU()
    S.FZFile = '/home/heliu/work/I9_test_data/FIT/DataFiles/HexFZ.dat'
    S.expDataInitial = '/home/heliu/work/I9_test_data/Integrated/S18_z1_'
    S.expdataNDigit = 6
    S.create_square_mic([10,10],voxelsize=0.01)
    S.squareMicOutFile = 'SearchBatchSize_13000_10x10_0.01_tex_mem_run0.npy'
    S.searchBatchSize = 13000
    S.recon_prepare()
    S.serial_recon_layer()
    #S.serial_recon_expansion_mode(S.squareMicData.shape[0]*S.squareMicData.shape[1]/2 + S.squareMicData.shape[1]/2)
if __name__ == "__main__":
    #S = Reconstructor_GPU()
    test_tex_mem()
    #context.detach()
    #cuda.stop_profiler()
    #grain_boundary()
    #recon_example()
    #squareMicMIsOrienMap()