# Copyright 2017 Tom Eulenfeld, GPLv3
"""Preprocessing and correlation"""
import itertools
import logging

import numpy as np
import obspy
from obspy.core import Stream
from obspy.geodetics import gps2dist_azimuth
from obspy.signal.cross_correlation import correlate as obscorr
from scipy.fftpack import fft, ifft, fftshift, ifftshift, next_fast_len
from scipy.signal import freqz, iirfilter, hilbert

from yam._from_msnoise import check_and_phase_shift
from yam.util import _filter, IterTime, smooth as smooth_func, _time2sec
import yam.stack


log = logging.getLogger('yam.correlate')


def _fill_array(data, mask=None, fill_value=None):
    """
    Fill masked numpy array with value without demasking.

    Additionally set fill_value to value.
    If data is not a MaskedArray returns silently data.
    """
    # TODO: get some gappy data and check if all calls to this function are
    # really necessary
    if mask is not None and mask is not False:
        data = np.ma.MaskedArray(data, mask=mask, copy=False)
    if np.ma.is_masked(data) and fill_value is not None:
        data._data[data.mask] = fill_value
        np.ma.set_fill_value(data, fill_value)
    elif not np.ma.is_masked(data):
        data = np.ma.filled(data)
    return data


def time_norm(data, method, clip_factor=1, clip_set_zero=False,
              mute_parts=48, mute_factor=2):
    """
    Calculate normalized data, see e.g. Bensen et al. (2007)

    :param data: numpy array with data to manipulate
    :param str method:
        1bit: reduce data to +1 if >0 and -1 if <0\n
        clip: clip data to the root mean square (rms)\n
        mute_envelope: calculate envelope and set data to zero where envelope
        is larger than specified
    :param float clip_factor: multiply std with this value before cliping
    :param bool clip_mask: instead of clipping, set the values to zero and mask
        them
    :param int mute_parts: mean of the envelope is calculated by dividing the
        envelope into several parts, the mean calculated in each part and
        the median of this averages defines the mean envelope
    :param float mute_factor: mean of envelope multiplied by this
        factor defines the level for muting

    :return: normalized data
    """
    mask = np.ma.getmask(data)
    if method == '1bit':
        data = np.sign(data)
    elif method == 'clip':
        std = np.std(data)
        args = (data < -clip_factor * std, data > clip_factor * std)
        if clip_set_zero:
            ind = np.logical_or(*args)
            data[ind] = 0
        else:
            np.clip(data, *args, out=data)
    elif method == 'mute_envelope':
        N = next_fast_len(len(data))
        envelope = np.abs(hilbert(data, N))[:len(data)]
        levels = [np.mean(d) for d in np.array_split(envelope, mute_parts)]
        level = mute_factor * np.median(levels)
        data[envelope > level] = 0
    else:
        msg = 'The method passed to time_norm is not known: %s.' % method
        raise ValueError(msg)
    return _fill_array(data, mask=mask, fill_value=0.)


# http://azitech.wordpress.com/
# 2011/03/15/designing-a-butterworth-low-pass-filter-with-scipy/
def _filter_resp(freqmin, freqmax, corners=2, zerophase=False, sr=None,
                 N=None, whole=False):
    """
    Complex frequency response of Butterworth-Bandpass Filter.

    :param freqmin: Pass band low corner frequency.
    :param freqmax: Pass band high corner frequency.
    :param corners: Filter corners
    :param zerophase: If True, apply filter once forwards and once backwards.
        This results in twice the number of corners but zero phase shift in
        the resulting filtered trace.
    :param sr: Sampling rate in Hz.
    :param N,whole: passed to scipy.signal.freqz

    :return: frequencies and complex response
    """
    df = sr
    fe = 0.5 * df
    low = freqmin / fe
    high = freqmax / fe
    # raise for some bad scenarios
    if high > 1:
        high = 1.0
        msg = "Selected high corner frequency is above Nyquist. " + \
              "Setting Nyquist as high corner."
        log.warning(msg)
    if low > 1:
        msg = "Selected low corner frequency is above Nyquist."
        raise ValueError(msg)
    [b, a] = iirfilter(corners, [low, high], btype='band',
                       ftype='butter', output='ba')
    freqs, values = freqz(b, a, N, whole=whole)
    if zerophase:
        values *= np.conjugate(values)
    return freqs, values


def spectral_whitening(data, sr=None, smooth=None, filter=None,
                       waterlevel=1e-8):
    """
    Apply spectral whitening to data

    Data is divided by its smoothed (Default: None) amplitude spectrum.

    :param data: numpy array with data to manipulate
    :param sr: sampling rate (only needed for smoothing)
    :param smooth: length of smoothing window in Hz
        (default None -> no smoothing)
    :param filter: filter spectrum with bandpass after whitening
        (tuple with min and max frequency)
    :param waterlevel: waterlevel relative to mean of spectrum

    :return: whitened data
    """
    mask = np.ma.getmask(data)
    N = len(data)
    nfft = next_fast_len(N)
    spec = fft(data, nfft)
    spec_ampl = np.sqrt(np.abs(np.multiply(spec, np.conjugate(spec))))
    if smooth:
        smooth = int(smooth * N / sr)
        spec_ampl = ifftshift(smooth_func(fftshift(spec_ampl), smooth))
    # save guard against division by 0
    wl = waterlevel * np.mean(spec_ampl)
    spec_ampl[spec_ampl < wl] = wl
    spec /= spec_ampl
    if filter is not None:
        spec *= _filter_resp(*filter, sr=sr, N=len(spec), whole=True)[1]
    ret = np.real(ifft(spec, nfft)[:N])
    return _fill_array(ret, mask=mask, fill_value=0.)


def correlate_traces(tr1, tr2, maxshift=3600):
    """
    Return trace of cross-correlation of two input traces

    :param tr1,tr2: two |Trace| objects
    :param maxsift: maximal shift in correlation in seconds
    """
    n1, s1, l1, c1 = tr1.id.split('.')
    n2, s2, l2, c2 = tr2.id.split('.')
    sr = tr1.stats.sampling_rate
    xdata = obscorr(tr1.data, tr2.data, int(round(maxshift * sr)))
    header = {'network': s1, 'station': c1, 'location': s2, 'channel': c2,
              'network1': n1, 'station1': s1, 'location1': l1, 'channel1': c1,
              'network2': n2, 'station2': s2, 'location2': l2, 'channel2': c2,
              'starttime': tr1.stats.starttime,
              'sampling_rate': sr,
              }
    return obspy.Trace(data=xdata, header=header)


def __get_stations(inventory):
    channels = inventory.get_contents()['channels']
    stations = sorted({ch[:-1] + '?': ch[-1] for ch in channels})
    return stations


def _iter_station_meta(inventory, components):
    """
    Return iterator yielding metadata per station and day.

    :param inventory: |Inventory| object with station and channel information
    :param components: components to yield
    """
    stations = __get_stations(inventory)
    for seedid in stations:
        for comp in components:
            net, sta, loc, cha = seedid.split('.')
            cha = cha[:2] + comp
            meta = {'network': net, 'station': sta, 'location': loc,
                    'channel': cha}
            yield meta


def get_data(smeta, data, data_format, day, overlap=0, edge=0,
             trim_and_merge=False):
    """Return data of one day

    :param smeta: dictionary with station metadata
    :param data: string with expression of data day files or
        function that returns the data (aka get_waveforms)
    :param data_format: format of data
    :param day: day as |UTC| object
    :param overlap: overlap to next day in seconds
    :param edge: additional time span requested from day before and after
        in seconds
    :param trim_and_merge: weather data is trimmed to day boundaries and merged
    """
    next_day = day + 24 * 3600
    if not isinstance(data, str):
        try:
            stream = data(starttime=day - edge,
                          endtime=next_day + overlap + edge, **smeta)
        except Exception as ex:
            log.debug('no data for %s %s: %s', day, smeta, str(ex))
            return
    else:
        fname = data.format(t=day, **smeta)
        try:
            stream = obspy.read(fname, data_format)
        except:
            return
        t1 = stream[0].stats.starttime
        t2 = stream[-1].stats.endtime
        if t1 - day < 60:
            fname = data.format(t=day - 1, **smeta)
            try:
                stream += obspy.read(fname, data_format, starttime=day - edge)
            except:
                pass
        if next_day - t2 < 60:
            endtime = next_day + overlap + edge
            fname = data.format(t=next_day, **smeta)
            try:
                stream += obspy.read(fname, data_format, endtime=endtime)
            except:
                pass
    if trim_and_merge:
        stream.merge(method=1, interpolation_samples=10)
        stream.trim(day, next_day + overlap)
    return stream


def preprocess(stream, day=None, inventory=None,
               overlap=0,
               remove_response=False,
               remove_response_options=None,
               filter=None,
               normalization=(),
               time_norm_options=None,
               spectral_whitening_options=None,
               downsample_before=None, downsample=None):
    """
    Preprocess stream of 1 day

    :param stream: |Stream| object
    :param day: |UTC| object of day (for trimming)
    :param inventory: |Inventory| object (for response removal)
    :param bool remove_response: remove response
    :param filter: min and max frequency of bandpass filter
    :param normalizaton: ordered list of normalizations to apply,
        ``'sprectal_whitening'`` for `spectral_whitening` and/or
        one or several of the time normalizations listed in `time_norm`
    :param downsample_before: downsample before preprocessing,
        target frequency
    :param downsample: downsample after preprocessing,
        target frequency
    :param \*_options: dictionary of options passed to the corresponding
        function
    """
    if time_norm_options is None:
        time_norm_options = {}
    if spectral_whitening_options is None:
        spectral_whitening_options = {}
    if remove_response_options is None:
        remove_response_options = {}
    if isinstance(normalization, str):
        normalization = [normalization]
    for tr in stream:
        tr.data = tr.data.astype('float64')
    if downsample_before:
        for tr in stream:
            if tr.stats.sampling_rate % downsample_before == 0:
                tr.decimate(int(tr.stats.sampling_rate) // downsample_before)
            else:
                tr.interpolate(downsample_before, method='lanczos')
    stream.traces[:] = [tr for tr in stream if len(tr) > 9]
    for tr in stream:
        tr.detrend()
        check_and_phase_shift(tr)
    if remove_response:
        stream.remove_response(inventory, **remove_response_options)
    for tr in stream:
        if filter is not None:
            _filter(tr, filter)
        for norm in normalization:
            if norm == 'spectral_whitening':
                sr = tr.stats.sampling_rate
                tr.data = spectral_whitening(tr.data, sr=sr,
                                             **spectral_whitening_options)
            else:
                tr.data = time_norm(tr.data, norm, **time_norm_options)
        if downsample:
            if tr.stats.sampling_rate % downsample == 0:
                tr.decimate(int(tr.stats.sampling_rate) // downsample)
            else:
                tr.interpolate(downsample, method='lanczos')
    if len({tr.stats.sampling_rate for tr in stream}) > 1:
        raise NotImplementedError('Different sampling rates')
    stream.merge(method=1, interpolation_samples=10)
    if day is not None:
        next_day = day + 24 * 3600
        stream.trim(day, next_day + overlap)
    stream.sort()


def correlate(io, day, outkey,
              edge=60,
              length=3600, overlap=1800,
              discard=None,
              only_auto_correlation=False,
              station_combinations=None,
              component_combinations=('ZZ',),
              max_lag=100,
              keep_correlations=False,
              stack='1day',
              **preprocessing_kwargs):
    """
    Correlate data of one day

    :param io: io config dictionary
    :param day: |UTC| object with day
    :param outkey: the output key for the HDF5 index
    :param edge: additional time span requested from day before and after
        in seconds
    :param length: length of correlation in seconds (string possible)
    :param overlap: length of overlap in seconds (string possible)
    :param discard: discard correlations with less data coverage
        (float from interval [0, 1])
    :param only_auto_correlations: Only correlate stations with itself
        (different components possible)
    :param station_combinations: specify station combinations
        (e.g. ``'CX.PATCX-CX.PB01``, network code can be
        omitted, e.g. ``'PATCX-PB01'``, default: all)
    :param component_combinations: component combinations to calculate,
        tuple of strings with length two, e.g. ``('ZZ', 'ZN', 'RR')``,
        if `'R'` or `'T'` is specified, components will be rotated after
        preprocessing, default: all component combinations (not rotated)
    :param max_lag: max time lag in correlations in seconds
    :param keep_correlatons: write correlations into HDF5 file (dafault: False)
    :param stack: stack correlations and write stacks into HDF5 file
        (default: ``'1d'``, must be smaller than one day or one day)

        .. note::

            If you want to stack larger time spans
            use the separate stack command on correlations or stacked
            correlations.

    :param \*\*preprocessing_kwargs: all other kwargs are passed to
        `preprocess`

    """
    inventory = io['inventory']
    length = _time2sec(length)
    overlap = _time2sec(overlap)
    if not keep_correlations and stack is None:
        raise ValueError('keep_correlation is False and stack is None')
    components = set(''.join(component_combinations))
    # load data
    stream = obspy.Stream()
    for smeta in _iter_station_meta(inventory, components):
        stream2 = get_data(smeta, io['data'], io['data_format'], day,
                           overlap=overlap, edge=edge)
        if stream2:
            stream += stream2
    if len(stream) == 0:
        log.warning('empty stream for day %s', str(day)[:10])
        return
    preprocess(stream, day, inventory, overlap=overlap,
               **preprocessing_kwargs)
    # start correlation
    next_day = day + 24 * 3600
    stations = [tr.id[:-1] for tr in stream]
    log.debug(components)
    log.debug(stations)
    for station1, station2 in itertools.combinations_with_replacement(
            stations, 2):
        if only_auto_correlation and station1 != station2:
            continue
        if station_combinations and not any(set(station_comb.split('-')) == (
                {station1.rsplit('.', 2)[0], station2.rsplit('.', 2)[0]}
                if '.' in (station_comb) else
                {station1.split('.')[1], station2.split('.')[1]})
                for station_comb in station_combinations):
            continue
        stream1 = Stream([tr for tr in stream if tr.id[:-1] == station1])
        stream2 = Stream([tr for tr in stream if tr.id[:-1] == station2])
        c1 = inventory.get_coordinates(
            stream1[0].id, datetime=stream1[0].stats.endtime)
        c2 = inventory.get_coordinates(
            stream2[0].id, datetime=stream2[0].stats.endtime)
        args = (c1['latitude'], c1['longitude'],
                c2['latitude'], c2['longitude'])
        dist, azi, baz = gps2dist_azimuth(*args)
        if 'R' in components or 'T' in components and station1 != station2:
            stream1 = stream1.copy()
            stream1b = stream1.copy().rotate('NE->RT', azi)
            stream1.extend(stream1b.select(channel='R'))
            stream1.extend(stream1b.select(channel='T'))
            stream2 = stream2.copy()
            stream2b = stream2.copy().rotate('NE->RT', azi)
            stream2.extend(stream2b.select(channel='R'))
            stream2.extend(stream2b.select(channel='T'))
        it_ = (itertools.product(stream1, stream2) if station1 != station2 else
               itertools.combinations_with_replacement(stream1, 2))
        for tr1, tr2 in it_:
            comps = tr1.stats.channel[-1] + tr2.stats.channel[-1]
            if component_combinations and (
                    comps not in component_combinations and
                    comps[::-1] not in component_combinations):
                continue
            xstream = obspy.Stream()
            for t1 in IterTime(day, next_day - length + overlap,
                               dt=length - overlap):
                sub = obspy.Stream([tr1, tr2]).slice(t1, t1 + length)
                if len(sub) < 2:
                    continue
                st = [tr.stats.starttime for tr in sub]
                et = [tr.stats.endtime for tr in sub]
                if max(st) > min(et):
                    continue
                sub.trim(max(st), min(et))
                if discard and any(
                        (tr.data.count() if hasattr(tr.data, 'count')
                         else len(tr))
                        / tr.stats.sampling_rate / length < discard
                        for tr in sub):
                    continue
                for tr in sub:
                    _fill_array(tr.data, fill_value=0.)
                    tr.data = np.ma.getdata(tr.data)
                xtr = correlate_traces(sub[0], sub[1], max_lag)
                xtr.stats.starttime = t1
                xtr.stats.key = outkey
                xtr.stats.dist = dist
                xtr.stats.azi = azi
                xtr.stats.baz = baz
                xstream += xtr
            # write and/or stack stream
            if len(xstream) > 0:
                if keep_correlations:
                    correlate.q.put((xstream, io['corr']))
                if stack:
                    xstack = yam.stack.stack(xstream, stack)
                    correlate.q.put((xstack, io['stack']))

#    Loop before introduction of rotation
#    for tr1, tr2 in itertools.combinations_with_replacement(stream, 2):
#        # skip unwanted combinations
#        station1 = tr1.stats.network + '.' + tr1.stats.station
#        station2 = tr2.stats.network + '.' + tr2.stats.station
#        comps = tr1.stats.channel[-1] + tr2.stats.channel[-1]
#        if only_auto_correlation and station1 != station2:
#            continue
#        if station_combinations and not any(set(station_comb.split('-')) == (
#                {station1, station2} if '.' in (station_comb) else
#                {tr1.stats.station, tr2.stats.station})
#                for station_comb in station_combinations):
#            continue
#        if component_combinations and (
#                comps not in component_combinations and
#                comps[::-1] not in component_combinations):
#            continue
#        # calculate distance and azimuth
#        c1 = inventory.get_coordinates(tr1.id, datetime=tr1.stats.endtime)
#        c2 = inventory.get_coordinates(tr2.id, datetime=tr2.stats.endtime)
#        args = (c1['latitude'], c1['longitude'],
#                c2['latitude'], c2['longitude'])
#        dist, azi, baz = gps2dist_azimuth(*args)
#        # correlate sliding streams
#        xstream = obspy.Stream()
#        for t1 in IterTime(day, next_day - length + overlap,
#                           dt=length - overlap):
#            sub = obspy.Stream([tr1, tr2]).slice(t1, t1 + length)
#            if len(sub) < 2:
#                continue
#            st = [tr.stats.starttime for tr in sub]
#            et = [tr.stats.endtime for tr in sub]
#            if max(st) > min(et):
#                continue
#            sub.trim(max(st), min(et))
#            if discard and any(
#                    (tr.data.count() if hasattr(tr.data, 'count') else len(tr))
#                    / tr.stats.sampling_rate / length < discard for tr in sub):
#                continue
#            for tr in sub:
#                _fill_array(tr.data, fill_value=0.)
#                tr.data = np.ma.getdata(tr.data)
#            xtr = correlate_traces(sub[0], sub[1], max_lag)
#            xtr.stats.starttime = t1
#            xtr.stats.key = outkey
#            xtr.stats.dist = dist
#            xtr.stats.azi = azi
#            xtr.stats.baz = baz
#            xstream += xtr
#        # write and/or stack stream
#        if len(xstream) > 0:
#            if keep_correlations:
#                correlate.q.put((xstream, io['corr']))
#            if stack:
#                xstack = yam.stack.stack(xstream, stack)
#                correlate.q.put((xstack, io['stack']))
