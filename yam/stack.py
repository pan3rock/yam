# Copyright 2017 Tom Eulenfeld, GPLv3
import numpy as np
import obspy
from obspy import UTCDateTime as UTC

from yam.util import _corr_id, _time2sec, IterTime


def stack(stream, length=None, move=None):
    stream.sort(keys=['starttime'])
    stream_stack = obspy.Stream()
    ids = {_corr_id(tr) for tr in stream}
    for id_ in ids:
        traces = [tr for tr in stream if _corr_id(tr) == id_]
        if length is None:
            data = np.mean([tr.data for tr in traces], axis=0)
            tr_stack = obspy.Trace(data, header=traces[0].stats)
            tr_stack.stats.key = tr_stack.stats.key + '_s'
            stream_stack.append(tr_stack)
        else:
            t1 = traces[0].stats.starttime
            lensec = _time2sec(length)
            movesec = _time2sec(move) if move else lensec
            if (lensec % (24 * 3600) == 0 or
                    isinstance(length, str) and 'd' in length):
                t1 = UTC(t1.year, t1.month, t1.day)
            elif (lensec % 3600 == 0  or
                    isinstance(length, str) and 'm' in length):
                t1 = UTC(t1.year, t1.month, t1.day, t1.hour)
            t2 = max(t1, traces[-1].stats.endtime - lensec)
            for t in IterTime(t1, t2, dt=movesec):
                sel = [tr for tr in traces
                       if -0.1 <= tr.stats.starttime - t <= lensec + 0.1]
                if len(sel) == 0:
                    continue
                data = np.mean([tr.data for tr in sel], axis=0)
                tr_stack = obspy.Trace(data, header=sel[0].stats)
                key_add = '_s%s' % length + (move is not None) * ('m%s' % move)
                tr_stack.stats.key = tr_stack.stats.key + key_add
                tr_stack.stats.starttime = t
                if 'num' in sel[0].stats:
                    tr_stack.stats.num = [tr.stats.num for tr in sel]
                else:
                    tr_stack.stats.num = len(sel)
                stream_stack.append(tr_stack)
    return stream_stack