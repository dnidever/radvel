#!/usr/bin/env python

"""RV.PY - Generic Radial Velocity Software

"""

from __future__ import print_function

__authors__ = 'David Nidever <dnidever@noao.edu>'
__version__ = '20190622'  # yyyymmdd                                                                                                                           

import os
#import sys, traceback
import contextlib, io, sys
import numpy as np
import warnings
from astropy.io import fits
from astropy.table import Table
import astropy.units as u
from astropy.time import Time
from astropy.coordinates import SkyCoord, EarthLocation
from astropy.wcs import WCS
from scipy.ndimage.filters import median_filter,gaussian_filter1d
from scipy.optimize import curve_fit, least_squares
from scipy.interpolate import interp1d
import thecannon as tc
from dlnpyutils import utils as dln, bindata
from .spec1d import Spec1D
from . import (cannon,utils)
import copy
import emcee
import corner
import logging
import time
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.legend import Legend

# Ignore these warnings, it's a bug
warnings.filterwarnings("ignore", message="numpy.dtype size changed")
warnings.filterwarnings("ignore", message="numpy.ufunc size changed")

cspeed = 2.99792458e5  # speed of light in km/s

def xcorr_dtype(nlag):
    """Return the dtype for the xcorr structure"""
    dtype = np.dtype([("xshift0",float),("ccp0",float),("xshift",float),("xshifterr",float),
                      ("xshift_interp",float),("ccf",(float,nlag)),("ccferr",(float,nlag)),("ccnlag",int),
                      ("cclag",(int,nlag)),("ccpeak",float),("ccpfwhm",float),("ccp_pars",(float,4)),
                      ("ccp_perror",(float,4)),("ccp_polycoef",(float,4)),("vrel",float),
                      ("vrelerr",float),("w0",float),("dw",float),("chisq",float)])
    return dtype

# astropy.modeling can handle errors and constraints


# https://stackoverflow.com/questions/2828953/silence-the-stdout-of-a-function-in-python-without-trashing-sys-stdout-and-resto
# usage:
#  with mute():
#    foo()
@contextlib.contextmanager
def mute():
    '''Prevent print to stdout, but if there was an error then catch it and
    print the output before raising the error.'''

    saved_stdout = sys.stdout
    saved_stderr = sys.stderr
    sys.stdout = io.StringIO()
    sys.stderr = io.StringIO()    
    try:
        yield
    except Exception:
        saved_output = sys.stdout
        saved_outerr = sys.stderr
        sys.stdout = saved_stdout
        sys.stderr = saved_stderr
        print(saved_output.getvalue())
        print(saved_outerr.getvalue())        
        raise
    sys.stdout = saved_stdout
    sys.stderr = saved_stderr

    
def ccorrelate(x, y, lag, yerr=None, covariance=False, double=None, nomean=False):
    """This function computes the cross correlation of two samples.

    This function computes the cross correlation Pxy(L) or cross
    covariance Rxy(L) of two sample populations X and Y as a function
    of the lag (L).

    This was translated from APC_CORRELATE.PRO which was itself a
    modification to the IDL C_CORRELATE.PRO function.

    Parameters
    ----------
    x : array
      The first array to cross correlate (e.g., the template).  If y is 2D
      (e.g., [Npix,Norder]), then x can be either 2D or 1D.  If x is 1D, then
      the cross-correlation is performed between x and each order of y and
      a 2D array will be output.  If x is 2D, then the cross-correlation
      of each order of x and y is performed and the results combined.
    y : array
      The second array to cross correlate.  Must be the same lenght as x.
      Can be 2D (e.g., [Npix, Norder]), but the shifting is always done
      on the 1st dimension.
    lag : array
      Vector that specifies the absolute distance(s) between
             indexed elements of x in the interval [-(n-2), (n-2)].
    yerr : array, optional
       Array of uncertainties in y.  Must be the same shape as y.
    covariange : bool
        If true, then the sample cross covariance is computed.

    Returns
    -------
    cross : array
         The cross correlation or cross covariance.
    cerror : array
         The uncertainty in "cross".  Only if "yerr" is input.

    Example
    -------

    Define two n-element sample populations.

    .. code-block:: python

         x = [3.73, 3.67, 3.77, 3.83, 4.67, 5.87, 6.70, 6.97, 6.40, 5.57]
         y = [2.31, 2.76, 3.02, 3.13, 3.72, 3.88, 3.97, 4.39, 4.34, 3.95]

    Compute the cross correlation of X and Y for LAG = -5, 0, 1, 5, 6, 7

    .. code-block:: python

         lag = [-5, 0, 1, 5, 6, 7]
         result = ccorrelate(x, y, lag)

    The result should be:

    .. code-block:: python

         [-0.428246, 0.914755, 0.674547, -0.405140, -0.403100, -0.339685]

    """


    # Compute the sample cross correlation or cross covariance of
    # (Xt, Xt+l) and (Yt, Yt+l) as a function of the lag (l).

    xshape = x.shape
    yshape = y.shape
    nx = xshape[0]
    if x.ndim==1:
        nxorder = 1
    else:
        nxorder = xshape[1]
    ny = yshape[0]
    if y.ndim==1:
        nyorder = 1
    else:
        nyorder = yshape[1]
    npix = nx
    norder = np.maximum(nxorder,nyorder)

    # Check the inputs
    if (nx != len(y)):
        raise ValueError("X and Y arrays must have the same number of pixels in 1st dimension.")

    if (x.ndim>2) | (y.ndim>2):
        raise ValueError("X and Y must be 1D or 2D.")

    if (x.ndim==2) & (y.ndim==1):
        raise ValueError("If X is 2D then Y must be as well.")

    # If X and Y are 2D then their Norders must be the same
    if (x.ndim==2) & (y.ndim==2) & (nxorder!=nyorder):
        raise ValueError("If X and Y are 2D then their length in the 2nd dimension must be the same.")

    # Check that Y and Yerr have the same length
    if (y.shape != yerr.shape):
        raise ValueError("Y and Yerr must have the same shape.")
    
    if (nx<2):
        raise ValueError("X and Y arrays must contain 2 or more elements.")

    # Reshape arrays to [Npix,Norder], even if both are 1D
    xd = x.copy()
    yd = y.copy()
    if yerr is not None: yderr=yerr.copy()
    if (norder>1):
        if (x.ndim==1):
            # make multiple copies of X
            xd = yd.copy()*0.0
            for i in range(norder):
                xd[:,i] = x.copy()
    else:
        xd = xd.reshape(npix,1)
        yd = yd.reshape(npix,1)        
        yderr = yderr.reshape(npix,1)
        
    # Remove the means
    if nomean is False:
        for i in range(norder):
            xd[:,i] -= np.nanmean(xd[:,i])
            yd[:,i] -= np.nanmean(yd[:,i])

    # Set NaNs or Infs to 0.0, mask bad pixels
    fx = np.isfinite(xd)
    ngdx = np.sum(fx)
    nbdx = np.sum((fx==False))
    if nbdx>0: xd[(fx==False)]=0.0
    fy = np.isfinite(yd)
    if yerr is not None:
        fy &= (yderr<1e20)   # mask out high errors as well
    ngdy = np.sum(fy)
    nbdy = np.sum((fy==False))
    if nbdy>0:
        yd[(fy==False)]=0.0
        if yerr is not None: yderr[(fy==False)]=0.0
    nlag = len(lag)
    
    # Initialize the output arrays
    cross = np.zeros((nlag,norder),dtype=float)
    cross_error = np.zeros((nlag,norder),dtype=float)
    num = np.zeros((nlag,norder),dtype=int)  # number of "good" points at this lag        
    rmsx = np.zeros(norder,dtype=float)
    rmsy = np.zeros(norder,dtype=float)    
    
    # Loop over orders
    for i in range(norder):
        # Loop over lag points
        for k in range(nlag):
            # Note the reversal of the variables for negative lags.
            if lag[k]>0:
                cross[k,i] = np.sum(xd[0:nx-lag[k],i] * yd[lag[k]:,i])
                num[k,i] = np.sum(fx[0:nx-lag[k],i] * fy[lag[k]:,i]) 
                if yerr is not None:
                    cross_error[k,i] = np.sum( (xd[0:nx-lag[k],i] * yderr[lag[k]:,i])**2 )
            else:
                cross[k,i] =  np.sum(yd[0:nx+lag[k],i] * xd[-lag[k]:,i])
                num[k,i] = np.sum(fy[0:nx+lag[k],i] * fx[-lag[k]:,i])
                if yerr is not None:
                    cross_error[k,i] = np.sum( (yderr[0:nx+lag[k],i] * xd[-lag[k]:,i])**2 )
                    
        if (npix>2):
            rmsx[i] = np.sum(xd[fx[:,i],i]**2)
            if (rmsx[i]==0): rmsx[i]=1.0
            rmsy[i] = np.sum(yd[fy[:,i],i]**2)
            if (rmsy[i]==0): rmsy[i]=1.0
        else:
            rmsx[i] = 1.0
            rmsy[i] = 1.0
            
    # Both X and Y are 2D, sum data from multiple orders
    if (nxorder>1) & (nyorder>1):
        cross = np.sum(cross,axis=1).reshape(nlag,1)
        cross_error= np.sum(cross_error,axis=1).reshape(nlag,1)
        num = np.sum(num,axis=1).reshape(nlag,1)
        rmsx = np.sqrt(np.sum(rmsx,axis=0)).reshape(1)
        rmsy = np.sqrt(np.sum(rmsy,axis=0)).reshape(1)
        norder = 1
        nelements = npix*norder
    else:
        rmsx = np.sqrt(rmsx)
        rmsy = np.sqrt(rmsy)        
        nelements = npix
        
    # Normalizations
    for i in range(norder):
        # Normalize by number of "good" points
        cross[:,i] *= np.max(num[:,i])
        pnum = (num[:,i]>0)
        cross[pnum,i] /= num[pnum,i]  # normalize by number of "good" points
        # Take sqrt to finish adding errors in quadrature
        cross_error[:,i] = np.sqrt(cross_error[:,i])
        # normalize
        cross_error[:,i] *= np.max(num[:,i])
        cross_error[pnum,i] /= num[pnum,i]

        # Divide by N for covariance, or divide by variance for correlation.
        if covariance is True:
            cross[:,i] /= nelements
            cross_error[:,i] /= nelements
        else:
            cross[:,i] /= rmsx[i]*rmsy[i]
            cross_error[:,i] /= rmsx[i]*rmsy[i]


    # Flatten to 1D if norder=1
    if norder==1:
        cross = cross.flatten()
        cross_error = cross_error.flatten()
    
    if yerr is not None: return cross, cross_error
    return cross


def specxcorr(wave=None,tempspec=None,obsspec=None,obserr=None,maxlag=200,errccf=False,prior=None):
    """This measures the radial velocity of a spectrum vs. a template using cross-correlation.

    This program measures the cross-correlation shift between
    a template spectrum (can be synthetic or observed) and
    an observed spectrum (or multiple spectra) on the same
    logarithmic wavelength scale.

    Parameters
    ----------
    wave : array
          The wavelength array.
    tempspec :
          The template spectrum: normalized and on log-lambda scale.
    obsspec : array
           The observed spectra: normalized and sampled on tempspec scale.
    obserr : array
           The observed error; normalized and sampled on tempspec scale.
    maxlag : int
           The maximum lag or shift to explore.
    prior : array, optional 
           Set a Gaussian prior on the cross-correlation.  The first
           term is the central position (in pixel shift) and the
           second term is the Gaussian sigma (in pixels).

    Returns
    -------
    outstr : numpy structured array
           The output structure of the final derived RVs and errors.
    auto : array
           The auto-correlation function of the template

    Examples
    --------

    out = apxcorr(wave,tempspec,spec,err)
    
    """


    # Not enough inputs
    if (wave is None) | (tempspec is None) | (obsspec is None) | (obserr is None):
        raise ValueError('Syntax - out = apxcorr(wave,tempspec,spec,err,auto=auto)')
        return

    nwave = len(wave)
    
    # Are there multiple observed spectra
    if obsspec.ndim>1:
        nspec = obsspec.shape[1]
    else:
        nspec = 1
    
    # Set up the cross-correlation parameters
    #  this only gives +/-450 km/s with 2048 pixels, maybe use larger range
    nlag = 2*np.round(np.abs(maxlag))+1
    if ((nlag % 2) == 0): nlag +=1  # make sure nlag is odd
    dlag = 1
    minlag = -np.int(np.ceil(nlag/2))
    lag = np.arange(nlag)*dlag+minlag+1

    # Initialize the output structure
    outstr = np.zeros(1,dtype=xcorr_dtype(nlag))
    outstr["xshift"] = np.nan
    outstr["xshifterr"] = np.nan
    outstr["vrel"] = np.nan
    outstr["vrelerr"] = np.nan
    outstr["chisq"] = np.nan

    wobs = wave.copy()
    nw = len(wobs)
    spec = obsspec.copy()
    err = obserr.copy()
    template = tempspec.copy()

    # mask bad pixels, set to NAN
    sfix = (spec < 0.01)
    nsfix = np.sum(sfix)
    if nsfix>0: spec[sfix] = np.nan
    tfix = (template < 0.01)
    ntfix = np.sum(tfix)
    if ntfix>0: template[tfix] = np.nan
    
    # set cross-corrlation window to be good range + nlag
    #lo = (0 if (gd[0]-nlag)<0 else gd[0]-nlag)
    #hi = ((nw-1) if (gd[ngd-1]+nlag)>(nw-1) else gd[ngd-1]+nlag)

    nindobs = np.sum(np.isfinite(spec) == True)  # only finite values, in case any NAN
    nindtemp = np.sum(np.isfinite(template) == True)  # only finite values, in case any NAN    
    if (nindobs>0) & (nindtemp>0):
        # Cross-Correlation
        #------------------
        # Calculate the CCF uncertainties using propagation of errors
        # Make median filtered error array
        #   high error values give crazy values in ccferr
        obserr1 = err.copy()
        bderr = ((obserr1 > 1) | (obserr1 <= 0.0))
        nbderr = np.sum(bderr)
        ngderr = np.sum((bderr==False))
        if (nbderr > 0) & (ngderr > 1): obserr1[bderr]=np.median([obserr1[(bderr==False)]])
        obserr1 = median_filter(obserr1,51)
        ccf, ccferr = ccorrelate(template,spec,lag,obserr1)

        # Apply flat-topped Gaussian prior with unit amplitude
        #  add a broader Gaussian underneath so the rest of the
        #   CCF isn't completely lost
        if prior is not None:
            ccf *= np.exp(-0.5*(((lag-prior[0])/prior[1])**4))*0.8+np.exp(-0.5*(((lag-prior[0])/150)**2))*0.2
        
    else:   # no good pixels
        ccf = np.float(lag)*0.0
        if (errccf is True) | (nofit is False): ccferr=ccf

    # Remove the median
    ccf -= np.median(ccf)

    # Best shift
    best_shiftind0 = np.argmax(ccf)
    best_xshift0 = lag[best_shiftind0]
    #temp = shift( tout, best_xshift0)
    temp = np.roll(template, best_xshift0)

    # Find Chisq for each synthetic spectrum
    gdmask = (np.isfinite(spec)==True) & (np.isfinite(template)==True) & (spec>0.0) & (err>0.0) & (err < 1e5)
    ngdpix = np.sum(gdmask)
    if (ngdpix==0):
        raise Exception('Bad spectrum')
    chisq = np.sqrt( np.sum( (spec[gdmask]-template[gdmask])**2/err[gdmask]**2 )/ngdpix )
    
    outstr["chisq"] = chisq
    outstr["ccf"] = ccf
    outstr["ccferr"] = ccferr
    outstr["ccnlag"] = nlag
    outstr["cclag"] = lag    
    
    # Remove smooth background at large scales
    cont = gaussian_filter1d(ccf,100)
    ccf_diff = ccf-cont

    # Get peak of CCF
    best_shiftind = np.argmax(ccf_diff)
    best_xshift = lag[best_shiftind]
    
    # Fit ccf peak with a Gaussian plus a line
    #---------------------------------------------
    # Some CCF peaks are SOOO wide that they span the whole width
    # do the first one without background subtraction
    estimates0 = [ccf_diff[best_shiftind0], best_xshift0, 4.0, 0.0]
    lbounds0 = [1e-3, np.min(lag), 0.1, -np.inf]
    ubounds0 =  [np.inf, np.max(lag), np.max(lag), np.inf]
    pars0, cov0 = dln.gaussfit(lag,ccf_diff,estimates0,ccferr,bounds=(lbounds0,ubounds0))
    perror0 = np.sqrt(np.diag(cov0))

    # Fit the width
    #  keep height, center and constant constrained
    estimates1 = pars0
    estimates1[1] = best_xshift
    lbounds1 = [0.5*estimates1[0], best_xshift-4, 0.3*estimates1[2], dln.lt(np.min(ccf_diff),dln.lt(0,estimates1[3]-0.1)) ]
    ubounds1 =  [1.5*estimates1[0], best_xshift+4, 1.5*estimates1[2], dln.gt(np.max(ccf_diff)*0.5,estimates1[3]+0.1) ]
    lo1 = np.int(dln.gt(np.floor(best_shiftind-dln.gt(estimates1[2]*2,5)),0))
    hi1 = np.int(dln.lt(np.ceil(best_shiftind+dln.gt(estimates1[2]*2,5)),len(lag)))
    pars1, cov1 = dln.gaussfit(lag[lo1:hi1],ccf_diff[lo1:hi1],estimates1,ccferr[lo1:hi1],bounds=(lbounds1,ubounds1))
    yfit1 = dln.gaussian(lag[lo1:hi1],*pars1)
    perror1 = np.sqrt(np.diag(cov1))
    
    # Fefit and let constant vary more, keep width constrained
    estimates2 = pars1
    estimates2[1] = dln.limit(estimates2[1],np.min(lag),np.max(lag))    # must be in range
    estimates2[3] = np.median(ccf_diff[lo1:hi1]-yfit1) + pars1[3]
    lbounds2 = [0.5*estimates2[0], dln.limit(best_xshift-dln.gt(estimates2[2],1), np.min(lag), estimates2[1]-1),
                0.3*estimates2[2], dln.lt(np.min(ccf_diff),dln.lt(0,estimates2[3]-0.1)) ]
    ubounds2 = [1.5*estimates2[0], dln.limit(best_xshift+dln.gt(estimates2[2],1), estimates2[1]+1, np.max(lag)),
                1.5*estimates2[2], dln.gt(np.max(ccf_diff)*0.5,estimates2[3]+0.1) ]
    lo2 = np.int(dln.gt(np.floor( best_shiftind-dln.gt(estimates2[2]*2,5)),0))
    hi2 = np.int(dln.lt(np.ceil( best_shiftind+dln.gt(estimates2[2]*2,5)),len(lag)))
    pars2, cov2 = dln.gaussfit(lag[lo2:hi2],ccf_diff[lo2:hi2],estimates2,ccferr[lo2:hi2],bounds=(lbounds2,ubounds2))
    yfit2 = dln.gaussian(lag[lo2:hi2],*pars2)    
    perror2 = np.sqrt(np.diag(cov2))
    
    # Refit with even narrower range
    estimates3 = pars2
    estimates3[1] = dln.limit(estimates3[1],np.min(lag),np.max(lag))    # must be in range
    estimates3[3] = np.median(ccf_diff[lo1:hi1]-yfit1) + pars1[3]
    lbounds3 = [0.5*estimates3[0], dln.limit(best_xshift-dln.gt(estimates3[2],1), np.min(lag), estimates3[1]-1),
                0.3*estimates3[2], dln.lt(np.min(ccf_diff),dln.lt(0,estimates3[3]-0.1)) ]
    ubounds3 = [1.5*estimates3[0], dln.limit(best_xshift+dln.gt(estimates3[2],1), estimates3[1]+1, np.max(lag)),
                1.5*estimates3[2], dln.gt(np.max(ccf_diff)*0.5,estimates3[3]+0.1) ]    
    lo3 = np.int(dln.gt(np.floor(best_shiftind-dln.gt(estimates3[2]*2,5)),0))
    hi3 = np.int(dln.lt(np.ceil(best_shiftind+dln.gt(estimates3[2]*2,5)),len(lag)))
    pars3, cov3 = dln.gaussfit(lag[lo3:hi3],ccf_diff[lo3:hi3],estimates3,ccferr[lo3:hi3],bounds=(lbounds3,ubounds3))
    yfit3 = dln.gaussian(lag[lo3:hi3],*pars3)    
    perror3 = np.sqrt(np.diag(cov3))

    # This seems to fix high shift/sigma errors
    if (perror3[0]>10) | (perror3[1]>10):
        dlbounds3 = [0.5*estimates3[0], -10+pars3[1], 0.01, dln.lt(np.min(ccf_diff),dln.lt(0,estimates3[3]-0.1)) ]
        dubounds3 = [1.5*estimates3[0], 10+pars3[1], 2*pars3[2], dln.gt(np.max(ccf_diff)*0.5,estimates3[3]+0.1) ]
        dpars3, dcov3 = dln.gaussfit(lag[lo3:hi3],ccf_diff[lo3:hi3],pars3,ccferr[lo3:hi3],bounds=(dlbounds3,dubounds3))
        dyfit3 = dln.gaussian(lag[lo3:hi3],*pars3)    
        perror3 = np.sqrt(np.diag(dcov3))
        
    # Final parameters
    pars = pars3
    perror = perror3
    xshift = pars[1]
    xshifterr = perror[1]
    ccpfwhm_pix = pars[2]*2.35482  # ccp fwhm in pixels
    # v = (10^(delta log(wave))-1)*c
    dwlog = np.median(dln.slope(np.log10(wave)))
    ccpfwhm = ( 10**(ccpfwhm_pix*dwlog)-1 )*cspeed  # in km/s

    # Convert pixel shift to velocity
    #---------------------------------
    # delta log(wave) = log(v/c+1)
    # v = (10^(delta log(wave))-1)*c
    dwlog = np.median(dln.slope(np.log10(wave)))
    vrel = ( 10**(xshift*dwlog)-1 )*cspeed
    # Vrel uncertainty
    dvreldshift = np.log(10.0)*(10**(xshift*dwlog))*dwlog*cspeed  # derivative wrt shift
    vrelerr = dvreldshift * xshifterr
    
    # Make CCF structure and add to STR
    #------------------------------------
    outstr["xshift0"] = best_xshift
    outstr["ccp0"] = np.max(ccf)
    outstr["xshift"] = xshift
    outstr["xshifterr"] = xshifterr
    #outstr[i].xshift_interp = xshift_interp
    outstr["ccpeak"] = pars[0] 
    outstr["ccpfwhm"] = ccpfwhm  # in km/s
    outstr["ccp_pars"] = pars
    outstr["ccp_perror"] = perror
    #outstr[i].ccp_polycoef = polycoef
    outstr["vrel"] = vrel
    outstr["vrelerr"] = vrelerr
    outstr["w0"] = np.min(wave)
    outstr["dw"] = dwlog
    
    return outstr


def normspec(spec=None,ncorder=6,fixbadpix=True,noerrcorr=False,
             binsize=0.05,perclevel=95.0,growsky=False,nsky=5):
    """
    This program normalizes a spectrum.

    Parameters
    ----------
    spec : Spec1D object
           A spectrum object.  This at least needs
                to have a FLUX and WAVE attribute.
    ncorder : int, default=6
            The continuum polynomial order.  The default is 6.
    noerrcorr : bool, default=False
            Do not use a correction for the effects of the errors
            on the continuum measurement.  The default is to make
            this correction if errors are included.
    fixbadpix : bool, default=True
            Set bad pixels to the continuum
    binsize : float, default=0.05
            The binsize to use (in units of 900A) for determining
            the Nth percentile spectrum to fit with a polynomial.

    perclevel : float, default=95
            The Nth percentile to use to determine the continuum.

    Returns
    -------
    nspec : array
         The continuum normalized spectrum.
    cont : array
         The continuum array.
    masked : array
         A boolean array specifying if a pixel was masked (True) or not (False).

    Examples
    --------

    nspec,cont,masked = normspec(spec)

    """

    # Not enough inputs
    if spec is None:
        raise ValueError("""spec2 = normspec(spec,fixbadpix=fixbadpix,ncorder=ncorder,noerrcorr=noerrcorr,
                                             binsize=binsize,perclevel=perclevel)""")
    musthave = ['flux','err','mask','wave']
    for a in musthave:
        if hasattr(spec,a) is False:
            raise ValueError("spec object must have "+a)

    # Can only do 1D or 2D arrays
    if spec.flux.ndim>2:
        raise Exception("Flux can only be 1D or 2D arrays")
        
    # Do special processing if the input is 2D
    #  Loop over the shorter axis
    if spec.flux.ndim==2:
        nx, ny = spec.flux.shape
        nspec = np.zeros(spec.flux.shape)
        cont = np.zeros(spec.flux.shape)
        masked = np.zeros(spec.flux.shape,bool)
        if nx<ny:
            for i in range(nx):
                flux = spec.flux[i,:]
                err = spec.err[i,:]
                mask = spec.mask[i,:]
                wave = spec.wave[i,:]
                spec1 = Spec1D(flux)
                spec1.err = err
                spec1.mask = mask
                spec1.wave = wave
                nspec1, cont1, masked1 = normspec(spec1,fixbadpix=fixbadpix,ncorder=ncorder,noerrcorr=noerrcorr,
                                                  binsize=binsize,perclevel=perclevel)
                nspec[i,:] = nspec1
                cont[i,:] = cont1
                masked[i,:] = masked1                
        else:
            for i in range(ny):
                flux = spec.flux[:,i]
                err = spec.err[:,i]
                mask = spec.mask[:,i]
                wave = spec.wave[:,i]
                spec1 = Spec1D(flux)
                spec1.err = err
                spec1.mask = mask
                spec1.wave = wave
                nspec1, cont1, masked1 = normspec(spec1,fixbadpix=fixbadpix,ncorder=ncorder,noerrcorr=noerrcorr,
                                                  binsize=binsize,perclevel=perclevel)
                nspec[:,i] = nspec1
                cont[:,i] = cont1
                masked[:,i] = masked1                
        return (nspec,cont,masked)
                
        
    # Continuum Normalize
    #----------------------
    w = spec.wave.copy()
    x = (w-np.median(w))/(np.max(w*0.5)-np.min(w*0.5))  # -1 to +1
    y = spec.flux.copy()
    yerr = None
    if hasattr(spec,'err') is True:
        if spec.err is not None:
            yerr = spec.err.copy()
            
    # Get good pixels, and set bad pixels to NAN
    #--------------------------------------------
    gdmask = (y>0)        # need positive fluxes
    ytemp = y.copy()

    # Exclude pixels with mask=bad
    if hasattr(spec,'mask') is True:
        if spec.mask is not None:
            mask = spec.mask.copy()
            gdmask = (mask == 0)
    gdpix = (gdmask == 1)
    ngdpix = np.sum(gdpix)
    bdpix = (gdmask != 1)
    nbdpix = np.sum(bdpix)
    if nbdpix>0: ytemp[bdpix]=np.nan   # set bad pixels to NAN for now

    # First attempt at continuum
    #----------------------------
    # Bin the data points
    xr = [np.nanmin(x),np.nanmax(x)]
    bins = np.ceil((xr[1]-xr[0])/binsize)+1
    ybin, bin_edges, binnumber = bindata.binned_statistic(x,ytemp,statistic='percentile',
                                                          percentile=perclevel,bins=bins,range=None)
    xbin = bin_edges[0:-1]+0.5*binsize
    gdbin = np.isfinite(ybin)
    ngdbin = np.sum(gdbin)
    if ngdbin<(ncorder+1):
        raise Exception("Not enough good flux points to fit the continuum")
    # Fit with robust polynomial
    coef1 = dln.poly_fit(xbin[gdbin],ybin[gdbin],ncorder,robust=True)
    cont1 = dln.poly(x,coef1)

    # Subtract smoothed error from it to remove the effects
    #  of noise on the continuum measurement
    if (yerr is not None) & (noerrcorr is False):
        smyerr = dln.medfilt(yerr,151)                            # first median filter
        smyerr = dln.gsmooth(smyerr,100)                          # Gaussian smoothing
        coef_err = dln.poly_fit(x,smyerr,ncorder,robust=True)     # fit with robust poly
        #poly_err = dln.poly(x,coef_err)
        #cont1 -= 2*dln.poly_err   # is this right????
        med_yerr = np.median(smyerr)                          # median error
        cont1 -= 2*med_yerr

    # Second iteration
    #-----------------
    #  This helps remove some residual structure
    ytemp2 = ytemp/cont1
    ybin2, bin_edges2, binnumber2 = bindata.binned_statistic(x,ytemp2,statistic='percentile',
                                                             percentile=perclevel,bins=bins,range=None)
    xbin2 = bin_edges2[0:-1]+0.5*binsize
    gdbin2 = np.isfinite(ybin2)
    ngdbin2 = np.sum(gdbin2)
    if ngdbin2<(ncorder+1):
        raise Exception("Not enough good flux points to fit the continuum")
    # Fit with robust polynomial
    coef2 = dln.poly_fit(xbin2[gdbin2],ybin2[gdbin2],ncorder,robust=True)
    cont2 = dln.poly(x,coef2)

    # Subtract smoothed error again
    if (yerr is not None) & (noerrcorr is False):    
      cont2 -= med_yerr/cont1

    # Final continuum
    cont = cont1*cont2  # final continuum

    # "Fix" bad pixels
    if (nbdpix>0) & fixbadpix is True:
        y[bdpix] = cont[bdpix]

    # Create continuum normalized spectrum
    nspec = spec.flux.copy()/cont

    # Add "masked" array
    masked = np.zeros(spec.flux.shape,bool)
    if (fixbadpix is True) & (nbdpix>0):
        masked[bdpix] = True
    
    return (nspec,cont,masked)


def spec_resid(pars,wave,flux,err,models,spec):
    """
    This helper function calculates the residuals between an observed spectrum and a Cannon model spectrum.
    
    Parameters
    ----------
    pars : array
      Input parameters [teff, logg, feh, rv].
    wave : array
      Wavelength array for observed spectrum.
    flux : array
       Observed flux array.
    err : array
        Uncertainties in the observed flux.
    models : list of Cannon models
        List of Cannon models to use
    spec : Spec1D
        The observed spectrum.  Needed to run cannon.model_spectrum().

    Outputs
    -------
    resid : array
         Array of residuals between the observed flux array and the Cannon model spectrum.

    """
    m = cannon.model_spectrum(models,spec,teff=pars[0],logg=pars[1],feh=pars[2],rv=pars[3])
    if m is None:
        return np.repeat(1e30,len(flux))
    resid = (flux-m.flux)/err
    return resid 


def emcee_lnlike(theta, x, y, yerr, models, spec):
    """
    This helper function calculates the log likelihood for the MCMC portion of fit().
    
    Parameters
    ----------
    theta : array
      Input parameters [teff, logg, feh, rv].
    x : array
      Array of x-values for y.  Not really used.
    y : array
       Observed flux array.
    yerr : array
        Uncertainties in the observed flux.
    models : list of Cannon models
        List of Cannon models to use
    spec : Spec1D
        The observed spectrum.  Needed to run cannon.model_spectrum().

    Outputs
    -------
    lnlike : float
         The log likelihood value.

    """
    m = cannon.model_spectrum(models,spec,teff=theta[0],logg=theta[1],feh=theta[2],rv=theta[3])
    inv_sigma2 = 1.0/yerr**2
    return -0.5*(np.sum((y-m.flux)**2*inv_sigma2))


def emcee_lnprior(theta, models):
    """
    This helper function calculates the log prior for the MCMC portion of fit().
    It's a flat/uniform prior across the stellar parameter space covered by the
    Cannon models.
    
    Parameters
    ----------
    theta : array
      Input parameters [teff, logg, feh, rv].
    models : list of Cannon models
        List of Cannon models to use

    Outputs
    -------
    lnprior : float
         The log prior value.

    """
    for m in models:
        inside = True
        for i in range(3):
            inside &= (theta[i]>=m.ranges[i,0]) & (theta[i]<=m.ranges[i,1])
        inside &= (np.abs(theta[3]) <= 2000)
        if inside:
            return 0.0
    return -np.inf

def emcee_lnprob(theta, x, y, yerr, models, spec):
    """
    This helper function calculates the log probability for the MCMC portion of fit().
    
    Parameters
    ----------
    theta : array
      Input parameters [teff, logg, feh, rv].
    x : array
      Array of x-values for y.  Not really used.
    y : array
       Observed flux array.
    yerr : array
        Uncertainties in the observed flux.
    models : list of Cannon models
        List of Cannon models to use
    spec : Spec1D
        The observed spectrum.  Needed to run cannon.model_spectrum().

    Outputs
    -------
    lnprob : float
         The log probability value, which is the sum of the log prior and the
         log likelihood.

    """
    lp = emcee_lnprior(theta,models)
    if not np.isfinite(lp):
        return -np.inf
    return lp + emcee_lnlike(theta, x, y, yerr, models, spec)


def fit(spec,models=None,verbose=False,mcmc=False,figname=None,cornername=None):
    """
    Fit the spectrum.  Find the best RV and stellar parameters using the Cannon models.

    Parameters
    ----------
    spec : Spec1D object
         The observed spectrum to match.
    models : list of Cannon models, optional
         A list of Cannon models to use.  The default is to load all of the Cannon
         models in the data/ directory and use those.
    verbose : bool, optional
         Verbose output of the various steps.  This is False by default.
    mcmc : bool, optional
         Run Markov Chain Monte Carlo (MCMC) to get improved parameter uncertainties.
         This is False by default.
    figname : string, optional
         The filename for a diagnostic plot showing the observed spectrum, model
         spectrum and the best-fit parameters.
    cornername : string, optional
         The filename for a "corner" plot showing the posterior distributions from
         the MCMC run.

    Returns
    -------
    out : numpy structured array
         The output structured array of the final derived RVs, stellar parameters and errors.
    model : Spec1D object
         The best-fitting Cannon model spectrum (as Spec1D object).

    Usage
    -----
    >>>out, model = doppler.rv.fit(spec)

    """

    
    # Turn off the Cannon's info messages
    tclogger = logging.getLogger('thecannon.utils')
    tclogger.disabled = True

    t0 = time.time()
    
    # Step 1: Prepare the spectrum
    #-----------------------------
    # normalize and mask spectrum
    if spec.normalized is False: spec.normalize()
    if spec.mask is not None:
        # Set errors to high value, leave flux alone
        spec.err[spec.mask] = 1e30
    
    # Step 2: Load and prepare the Cannon models
    #-------------------------------------------
    if models is None: models = cannon.load_all_cannon_models() 
    #  NOT interpolated onto the observed wavelength scale
    pmodels = cannon.prepare_cannon_model(models,spec,dointerp=False)        

    #### I THINK THE WAVELENGTH AIR<->VACUUM CONVERSION SHOULD HAPPEN INSIDE
    #### PREPARE_CANON_MODEL().  IT COULD BE DIFFERENT FOR EACH MODEL.
    
    # Step 3: put on logarithmic wavelength grid
    #-------------------------------------------
    wavelog = utils.make_logwave_scale(spec.wave,vel=0.0)  # get new wavelength solution
    obs = spec.interp(wavelog)
    # The LSF information will not be correct if using Gauss-Hermite, it uses a Gaussian approximation
    # it's okay because the "pmodels" are prepared for the original spectra (above)
    
    # Step 4: get initial RV using cross-correlation with rough sampling of Teff/logg parameter space
    #------------------------------------------------------------------------------------------------
    dwlog = np.median(dln.slope(np.log10(wavelog)))
    # vrel = ( 10**(xshift*dwlog)-1 )*cspeed
    maxlag = np.int(np.ceil(np.log10(1+2000.0/cspeed)/dwlog))
    maxlag = np.maximum(maxlag,50)
    teff = [3500.0, 4000.0, 5000.0, 6000.0, 7500.0, 9000.0, 15000.0, 25000.0,  3500.0, 4300.0, 4700.0, 5200.0]
    logg = [4.8, 4.8, 4.6, 4.4, 4.0, 4.0, 4.0, 4.0,  0.5, 1.0, 2.0, 3.0]
    feh = -0.5
    outdtype = np.dtype([('xshift',np.float32),('vrel',np.float32),('vrelerr',np.float32),('ccpeak',np.float32),('ccpfwhm',np.float32),
                         ('chisq',np.float32),('teff',np.float32),('logg',np.float32),('feh',np.float32)])
    outstr = np.zeros(len(teff),dtype=outdtype)
    if verbose is True: print('TEFF    LOGG     FEH    VREL   CCPEAK    CHISQ')
    for i in range(len(teff)):
        m = cannon.model_spectrum(pmodels,obs,teff=teff[i],logg=logg[i],feh=feh,rv=0)
        outstr1 = specxcorr(m.wave,m.flux,obs.flux,obs.err,maxlag)
        if verbose is True:
            print('%-7.2f  %5.2f  %5.2f  %5.2f  %5.2f  %5.2f' % (teff[i],logg[i],feh,outstr1['vrel'][0],outstr1['ccpeak'][0],outstr1['chisq'][0]))
        for n in ['xshift','vrel','vrelerr','ccpeak','ccpfwhm','chisq']: outstr[n][i] = outstr1[n]
        outstr['teff'][i] = teff[i]
        outstr['logg'][i] = logg[i]
        outstr['feh'][i] = feh            
    # Get best fit
    #bestind = np.argmax(outstr['ccpeak'])
    bestind = np.argmin(outstr['chisq'])    
    beststr = outstr[bestind]

    if verbose is True:
        print('Initial RV fit:')
        print('Vrel   = %5.2f km/s' % beststr['vrel'])
        print('Vrelerr= %5.2f km/s' % beststr['vrelerr'])    
        print('Teff   = %5.2f K' % beststr['teff'])
        print('logg   = %5.2f' % beststr['logg'])
        print('[Fe/H] = %5.2f' % beststr['feh']) 

        
    # Step 5: Get better Cannon stellar parameters using initial RV
    #--------------------------------------------------------------
    # put observed spectrum on rest wavelength scale
    # get cannnon model for "best" teff/logg/feh values
    # run cannon.test() on the spectrum and variances
    # just shift the observed wavelengths to rest, do NOT interpolate the spectrum
    #restwave = obs.wave*(1-beststr['vrel']/cspeed)
    restwave = spec.wave*(1-beststr['vrel']/cspeed)    
    bestmodel = cannon.get_best_cannon_model(pmodels,[beststr['teff'],beststr['logg'],beststr['feh']])
    # Deal with multiple orders
    if spec.norder>1:
        bestmodelinterp = []
        for i in range(spec.norder):
            import pdb; pdb.set_trace()
            bestmodelinterp1 = cannon.interp_cannon_model(bestmodel[i],wout=restwave[:,i])
            bestmodelinterp.append(bestmodelinterp1)
    else:
        bestmodelinterp = cannon.interp_cannon_model(bestmodel,wout=restwave)

        
    # Need to "stack" the cannon models to perform a single fit
    if spec.norders>1:
        bestmodlinterp = cannon.hstack(bestmodelinterp)
        
    with mute():   # suppress output
        labels0, cov0, meta0 = bestmodelinterp.test(obs.flux, 1.0/obs.err**2)
    # Make sure the labels are within the ranges
    labels0 = labels0.flatten()
    for i in range(3): labels0[i]=dln.limit(labels0[i],bestmodelinterp.ranges[i,0],bestmodelinterp.ranges[i,1])
    bestmodelspec0 = bestmodelinterp(labels0)
    if verbose is True:
        print('Initial Cannon stellar parameters using initial RV')
        print('Teff   = %5.2f K' % labels0[0])
        print('logg   = %5.2f' % labels0[1])
        print('[Fe/H] = %5.2f' % labels0[2])    
    
    # Tweak the continuum normalization
    smlen = len(obs.flux)/20.0
    ratio = obs.flux/bestmodelspec0
    ratio[0] = np.median(ratio[0:np.int(smlen/2)])
    ratio[-1] = np.median(ratio[-np.int(smlen/2):-1])
    sm = dln.gsmooth(ratio,smlen,boundary='extend')
    obs.cont = sm
    obs.flux /= sm
    obs.err /= sm

    # Refit the Cannon
    with mute():    # suppress output
        labels, cov, meta = bestmodelinterp.test(obs.flux, 1.0/obs.err**2)
    # Make sure the labels are within the ranges
    labels = labels.flatten()
    for i in range(3): labels[i]=dln.limit(labels[i],bestmodelinterp.ranges[i,0],bestmodelinterp.ranges[i,1])
    bestmodelspec = bestmodelinterp(labels)
    if verbose is True:
        print('Initial Cannon stellar parameters using initial RV and Tweaking the normalization')
        print('Teff   = %5.2f K' % labels[0])
        print('logg   = %5.2f' % labels[1])
        print('[Fe/H] = %5.2f' % labels[2]) 
    

    # Step 6: Improved RV using better Cannon template
    #-------------------------------------------------
    m = cannon.model_spectrum(pmodels,obs,teff=labels[0],logg=labels[1],feh=labels[2],rv=0)
    outstr2 = specxcorr(m.wave,m.flux,obs.flux,obs.err,maxlag)
    beststr2= np.zeros(1,dtype=outdtype)
    for n in ['xshift','vrel','vrelerr','ccpeak','ccpfwhm','chisq']: beststr2[n] = outstr2[n]
    beststr2['teff'] = labels[0]
    beststr2['logg'] = labels[1]
    beststr2['feh'] = labels[2]


    # Step 7: Improved Cannon stellar parameters
    #-------------------------------------------
    restwave = obs.wave*(1-beststr2['vrel']/cspeed)
    bestmodel = cannon.get_best_cannon_model(pmodels,[beststr2['teff'],beststr2['logg'],beststr2['feh']])
    bestmodelinterp = cannon.interp_cannon_model(bestmodel,wout=restwave)
    with mute():     # suppress output
        labels2, cov2, meta2 = bestmodelinterp.test(obs.flux, 1.0/obs.err**2)
    # Make sure the labels are within the ranges
    labels2 = labels2.flatten()
    for i in range(3): labels2[i]=dln.limit(labels2[i],bestmodelinterp.ranges[i,0],bestmodelinterp.ranges[i,1])
    bestmodelspec2 = bestmodelinterp(labels2)
    if verbose is True:
        print('Improved RV and Cannon stellar parameters:')
        print('Vrel   = %5.2f km/s' % beststr2['vrel'])
        print('Vrelerr= %5.2f km/s' % beststr2['vrelerr']) 
        print('Teff   = %5.2f K' % labels2[0])
        print('logg   = %5.2f' % labels2[1])
        print('[Fe/H] = %5.2f' % labels2[2]) 

    
    # Step 8: Least Squares fitting with forward modeling
    #----------------------------------------------------
    # Tweak the normalization    
    m = cannon.model_spectrum(pmodels,spec,teff=beststr2['teff'],logg=beststr2['logg'],feh=beststr2['feh'],rv=beststr2['vrel'])
    smlen = len(spec.flux)/20.0
    ratio = spec.flux/m.flux
    ratio[0] = np.median(ratio[0:np.int(smlen/2)])
    ratio[-1] = np.median(ratio[-np.int(smlen/2):-1])
    sm = dln.gsmooth(ratio,smlen,boundary='extend')
    spec.cont *= sm
    spec.flux /= sm
    spec.err /= sm
    
    # Least squares with forward modeling
    #loss = 'linear'
    ##loss = 'soft_l1'
    #initpar = [beststr2['teff'],beststr2['logg'],beststr2['feh'],beststr2['vrel']]
    #initpar = np.array(initpar).flatten()
    #max_nfev = 100
    ##lbounds = [initpar[0]*0.5, initpar[1]*0.5, initpar[2]-0.5, initpar[3]-np.maximum(5*beststr2['vrelerr'][0],30.0)]
    ##ubounds = [initpar[0]*1.5, initpar[1]*1.5, initpar[2]+0.5, initpar[3]+np.maximum(5*beststr2['vrelerr'][0],30.0)]
    #fscale = 1.0  # 0.1
    #res = least_squares(spec_resid, initpar, loss=loss, f_scale=fscale, args=(spec.wave,spec.flux,spec.err,pmodels,spec),
    #                    max_nfev=max_nfev,method='lm')
    ## lm does not support bounds and only linear loss function
    #if res.success is False:
    #    print('Problems in least squares fitting')
    #    lspars = initpars
    #else:
    #    lspars = res.x
    #    print('Least Squares RV and stellar parameters:')
    #    print('Vrel   = %5.2f km/s' % lspars[3])
    #    print('Teff   = %5.2f K' % lspars[0])
    #    print('logg   = %5.2f' % lspars[1])
    #    print('[Fe/H] = %5.2f' % lspars[2]) 

    # function to use with curve_fit
    def spec_interp(x,teff,logg,feh,rv):
        """ This returns the interpolated model for a given spectrum."""
        # The "pmodels" and "spec" must already exist outside of this function
        m = cannon.model_spectrum(pmodels,spec,teff=teff,logg=logg,feh=feh,rv=rv)
        if m is None:
            return np.repeat(1e30,len(spec.flux))
        return m.flux
    
    # Use curve_fit
    initpar = [beststr2['teff'],beststr2['logg'],beststr2['feh'],beststr2['vrel']]
    initpar = np.array(initpar).flatten()
    lspars, lscov = curve_fit(spec_interp, spec.wave, spec.flux, p0=initpar, sigma=spec.err)
    lsperror = np.sqrt(np.diag(lscov))
    if verbose is True:
        print('Least Squares RV and stellar parameters:')
        print('Vrel   = %5.2f km/s' % lspars[3])
        print('Teff   = %5.2f K' % lspars[0])
        print('logg   = %5.2f' % lspars[1])
        print('[Fe/H] = %5.2f' % lspars[2]) 
    lsmodel = cannon.model_spectrum(models,spec,teff=lspars[0],logg=lspars[1],feh=lspars[2],rv=lspars[3])        
    lschisq = np.sqrt(np.sum(((spec.flux-lsmodel.flux)/spec.err)**2)/len(spec.flux))
    if verbose is True: print('chisq = %5.2f' % lschisq)

    # Step 9: Run fine grid in RV, forward modeling
    #----------------------------------------------
    maxv = np.maximum(beststr2['vrel'][0],20.0)
    vel = dln.scale_vector(np.arange(30),lspars[3]-maxv,lspars[3]+maxv)
    chisq = np.zeros(len(vel))
    for i,v in enumerate(vel):
        m = cannon.model_spectrum(pmodels,spec,teff=lspars[0],logg=lspars[1],feh=lspars[2],rv=v)
        chisq[i] = np.sqrt(np.sum(((spec.flux-m.flux)/spec.err)**2)/len(spec.flux))
    vel2 = dln.scale_vector(np.arange(300),lspars[3]-maxv,lspars[3]+maxv)
    chisq2 = dln.interp(vel,chisq,vel2)
    bestind = np.argmin(chisq2)
    finerv = vel2[bestind]
    finechisq = chisq2[bestind]
    if verbose is True:
        print('Fine grid best RV = %5.2f km/s' % finerv)
        print('chisq = %5.2f' % finechisq)

    # Final parameters and uncertainties (so far)
    fpars = lspars
    fperror = lsperror
    fpars[3] = finerv
    fchisq = finechisq
    fmodel = cannon.model_spectrum(models,spec,teff=lspars[0],logg=lspars[1],feh=lspars[2],rv=finerv) 

    
    # Step 9: MCMC
    #--------------
    if (mcmc is True) | (cornername is not None):
        ndim, nwalkers = 4, 20
        delta = [fpars[0]*0.1, 0.1, 0.1, 3*beststr['vrelerr']]
        pos = [fpars + delta*np.random.randn(ndim) for i in range(nwalkers)]

        sampler = emcee.EnsembleSampler(nwalkers, ndim, emcee_lnprob, args=(spec.wave, spec.flux, spec.err, pmodels, spec))

        steps = 100
        if cornername is not None: steps=500
        #%timeit out = sampler.run_mcmc(pos, 500)
        out = sampler.run_mcmc(pos, steps)

        samples = sampler.chain[:, np.int(steps/2):, :].reshape((-1, ndim))
        #samples = sampler.get_chain(discard=50, thin=1, flat=True)
        
        pars = np.zeros(ndim,float)
        parerr = np.zeros(ndim,float)
        if verbose is True: print('Final MCMC values:')
        names = ['Teff','logg','[Fe/H]','Vrel']
        for i in range(ndim):
            t=np.percentile(samples[:,i],[16,50,84])
            pars[i] = t[1]
            parerr[i] = (t[2]-t[0])*0.5
            if verbose is True: print(names[i]+' = %5.2f +/- %5.2f' % (pars[i],parerr[i]))

        #fig, axes = plt.subplots(4, figsize=(10, 7), sharex=True)
        #samples = sampler.get_chain()
        #labels = ["teff", "logg", "feh", "vrel"]
        #for i in range(ndim):
        #    ax = axes[i]
        #    ax.plot(samples[:, :, i], "k", alpha=0.3)
        #    ax.set_xlim(0, len(samples))
        #    ax.set_ylabel(labels[i])
        #    ax.yaxis.set_label_coords(-0.1, 0.5)
        #axes[-1].set_xlabel("step number");
            
        # The maximum likelihood parameters
        bestind = np.unravel_index(np.argmax(sampler.lnprobability),sampler.lnprobability.shape)
        pars_ml = sampler.chain[bestind[0],bestind[1],:]

        mcmodel = cannon.model_spectrum(models,spec,teff=pars[0],logg=pars[1],feh=pars[2],rv=pars[3])
        mcchisq = np.sqrt(np.sum(((spec.flux-mcmodel.flux)/spec.err)**2)/len(spec.flux))

        # Use these parameters
        fpars = pars
        fperror = parerr
        fchisq = mcchisq
        fmodel = mcmodel
        
        # Corner plot
        if cornername is not None:
            matplotlib.use('Agg')
            fig = corner.corner(samples, labels=["T$_eff$", "$\log{g}$", "[Fe/H]", "Vrel"], truths=fpars)        
            plt.savefig(cornername)
            plt.close(fig)
            print('Corner plot saved to '+cornername)
        

    # Construct the output
    #---------------------
    bc = spec.barycorr()
    vhelio = fpars[3] + bc
    if verbose is True:
        print('Vhelio = %5.2f km/s' % vhelio)
        print('BC = %5.2f km/s' % bc)
    dtype = np.dtype([('vhelio',np.float32),('vrel',np.float32),('vrelerr',np.float32),
                      ('teff',np.float32),('tefferr',np.float32),('logg',np.float32),('loggerr',np.float32),
                      ('feh',np.float32),('feherr',np.float32),('chisq',np.float32),('bc',np.float32)])
    out = np.zeros(1,dtype=dtype)
    out['vhelio'] = vhelio
    out['vrel'] = fpars[3]
    out['vrelerr'] = fperror[3]    
    out['teff'] = fpars[0]
    out['tefferr'] = fperror[0]    
    out['logg'] = fpars[1]
    out['tefferr'] = fperror[1]        
    out['feh'] = fpars[2]
    out['tefferr'] = fperror[2]    
    out['chisq'] = fchisq
    out['bc'] = bc


    # Make diagnostic figue
    if figname is not None:
        #import matplotlib
        matplotlib.use('Agg')
        #import matplotlib.pyplot as plt
        if os.path.exists(figname): os.remove(figname)
        fig,ax = plt.subplots()
        plt.plot(spec.wave,spec.flux,'b',label='Data')
        plt.plot(fmodel.wave,fmodel.flux,'--r',label='Model')
        #ax.axis('equal')
        leg = ax.legend(loc='upper left', frameon=False)
        plt.xlabel('Wavelength (Angstroms)')
        plt.ylabel('Normalized Flux')
        xr = dln.minmax(spec.wave)
        yr = [np.min([spec.flux,fmodel.flux]), np.max([spec.flux,fmodel.flux])]
        yr = [yr[0]-dln.valrange(yr)*0.05,yr[1]+dln.valrange(yr)*0.05]
        yr = [np.max([yr[0],-0.2]), np.min([yr[1],2.0])]
        plt.xlim(xr)
        plt.ylim(yr)
        # legend
        # best-fit pars: Teff, logg, [Fe/H], RV with uncertainties and Chisq
        #leg = Legend(ax, lines[2:], ['line C', 'line D'],
        #             loc='lower right', frameon=False)
        #ax.add_artist(leg)
        wr = dln.minmax(spec.wave)
        #plt.legend(('Teff', 'logg', '[Fe/H]','Vrel'),
        #           loc='upper left', handlelength=1.5, fontsize=16)
        ax.annotate(r'Teff=%5.1f$\pm$%5.1f  logg=%5.2f$\pm$%5.2f  [Fe/H]=%5.2f$\pm$%5.2f   Vrel=%5.2f$\pm$%5.2f ' %
                    (out['teff'], out['tefferr'], out['logg'], out['loggerr'], out['feh'], out['feherr'], out['vrel'], out['vrelerr']),
                    xy=(xr[0]+dln.valrange(xr)*0.05, yr[0]+dln.valrange(yr)*0.05))
        plt.savefig(figname)
        plt.close(fig)
        print('Figure saved to '+figname)

    # How long did this take
    if verbose is True: print('dt = %5.2f sec.' % (time.time()-t0))
        
    return out, fmodel

    
    # for multiple orders, have models be a list of lists.
    # but what if there's just ONE model, how to know if the single list is
    # over multiple models or multiple orders??
    # maybe add a parameter to the cannon model that's something like multipleorders=True / False
    # or norder=2 and order=0/1/2
    
    # to run cannon.test() on multiple orders, maybe "stack" the cannon
    # models (right next to each other) for each order so its like one large cannon model
    
    

def jointfit(spectra,models,mcmc=False):
    """This fits a Cannon model to multiple spectra of the same star."""

    # Would be great if the Cannon could fit one set of labels for multiple spectra.

    # Step 1) Loop through each spectrum and run fit()
    # Step 2) find weighted stellar parameters
    # Step 3) refit each spectrum using the best stellar parameters
    
    pass
