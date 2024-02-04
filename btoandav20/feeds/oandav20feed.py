from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

from datetime import datetime, timedelta, timezone, time

import time as _time
import threading
import logging

from backtrader.feed import DataBase
from backtrader import TimeFrame, date2num, num2date
from backtrader.utils.py3 import queue, with_metaclass

from btoandav20.stores import oandav20store

logger = logging.getLogger(__name__)


class MetaOandaV20Data(DataBase.__class__):
    def __init__(self, name, bases, dct):
        '''Class has already been created ... register'''
        # Initialize the class
        super(MetaOandaV20Data, self).__init__(name, bases, dct)

        # Register with the store
        oandav20store.OandaV20Store.DataCls = self


class OandaV20Data(with_metaclass(MetaOandaV20Data, DataBase)):

    '''Oanda v20 Data Feed.

    Params:

      - ``qcheck`` (default: ``0.5``)

        Time in seconds to wake up if no data is received to give a chance to
        resample/replay packets properly and pass notifications up the chain

      - ``historical`` (default: ``False``)

        If set to ``True`` the data feed will stop after doing the first
        download of data.

        The standard data feed parameters ``fromdate`` and ``todate`` will be
        used as reference.

        The data feed will make multiple requests if the requested duration is
        larger than the one allowed by IB given the timeframe/compression
        chosen for the data.

      - ``backfill_start`` (default: ``True``)

        Perform backfilling at the start. The maximum possible historical data
        will be fetched in a single request.

      - ``backfill`` (default: ``True``)

        Perform backfilling after a disconnection/reconnection cycle. The gap
        duration will be used to download the smallest possible amount of data

      - ``backfill_from`` (default: ``None``)

        An additional data source can be passed to do an initial layer of
        backfilling. Once the data source is depleted and if requested,
        backfilling from oanda will take place. This is ideally meant to
        backfill from already stored sources like a file on disk, but not
        limited to.

      - ``bidask`` (default: ``True``)

        If ``True``, then the historical/backfilling requests will request
        bid/ask prices from the server

        If ``False``, then *midpoint* will be requested

      - ``useask`` (default: ``False``)

        If ``True`` the *ask* part of the *bidask* prices will be used instead
        of the default use of *bid*

      - ``reconnect`` (default: ``True``)

        Reconnect when network connection is down

      - ``reconnections`` (default: ``-1``)

        Number of times to attempt reconnections: ``-1`` means forever

      - ``candles`` (default: ``False``)

        Return candles instead of streaming for current data, granularity
        needs to be higher than Ticks

      - ``candles_delay`` (default: ``1``)

        The delay in seconds when new candles should be fetched after a
        period is over


      - ``adjstarttime`` (default: ``False``)

        Allows to adjust the start time of a candle to the end of the
        candles period. This only affects backfill data and live data
        if candles=True


    This data feed supports only this mapping of ``timeframe`` and
    ``compression``, which comply with the definitions in the OANDA API
    Developer's Guide:

        (TimeFrame.Seconds, 5): 'S5',
        (TimeFrame.Seconds, 10): 'S10',
        (TimeFrame.Seconds, 15): 'S15',
        (TimeFrame.Seconds, 30): 'S30',
        (TimeFrame.Minutes, 1): 'M1',
        (TimeFrame.Minutes, 2): 'M3',
        (TimeFrame.Minutes, 3): 'M3',
        (TimeFrame.Minutes, 4): 'M4',
        (TimeFrame.Minutes, 5): 'M5',
        (TimeFrame.Minutes, 10): 'M10',
        (TimeFrame.Minutes, 15): 'M15',
        (TimeFrame.Minutes, 30): 'M30',
        (TimeFrame.Minutes, 60): 'H1',
        (TimeFrame.Minutes, 120): 'H2',
        (TimeFrame.Minutes, 180): 'H3',
        (TimeFrame.Minutes, 240): 'H4',
        (TimeFrame.Minutes, 360): 'H6',
        (TimeFrame.Minutes, 480): 'H8',
        (TimeFrame.Days, 1): 'D',
        (TimeFrame.Weeks, 1): 'W',
        (TimeFrame.Months, 1): 'M',

    Any other combination will be rejected
    '''

    lines = ('mid_close', 'bid_close', 'ask_close',)

    params = dict(
        qcheck=0.5,
        historical=False,     # do backfilling at the start
        backfill_start=True,  # do backfilling at the start
        backfill=True,        # do backfilling when reconnecting
        backfill_from=None,   # additional data source to do backfill from
        bidask=True,
        useask=False,
        candles=False,
        candles_delay=1,
        adjstarttime=False,   # adjust start time of candle
        # TODO readd tmout - set timeout in store
        reconnect=True,
        reconnections=-1,     # forever
    )

    _store = oandav20store.OandaV20Store

    # States for the Finite State Machine in _load
    _ST_FROM, _ST_START, _ST_LIVE, _ST_HISTORBACK, _ST_OVER = range(5)

    def islive(self):
        '''Returns ``True`` to notify ``Cerebro`` that preloading and runonce
        should be deactivated'''
        return True

    def __init__(self, **kwargs):
        self.o = self._store(**kwargs)
        self._candleFormat = 'ABM'

    def setenvironment(self, env):
        '''Receives an environment (cerebro) and passes it over to the store it
        belongs to'''
        super(OandaV20Data, self).setenvironment(env)
        env.addstore(self.o)

    def start(self):
        '''Starts the Oanda connection and gets the real contract and
        contractdetails if it exists'''
        super(OandaV20Data, self).start()

        # create attributes as soon as possible
        self._statelivereconn = False  # if reconnecting in live state
        self._storedmsg = dict()  # keep pending live message (under None)
        self.qlive = queue.Queue()
        self._state = self._ST_OVER
        self._reconns = self.p.reconnections
        self.contractdetails = None

        # kickstart store and get queue to wait on
        self.o.start(data=self)

        # check if the granularity is supported
        otf = self.o.get_granularity(self._timeframe, self._compression)
        if otf is None:
            self.put_notification(self.NOTSUPPORTED_TF)
            self._state = self._ST_OVER
            return

        self.contractdetails = cd = self.o.get_instrument(self.p.dataname)
        if cd is None:
            self.put_notification(self.NOTSUBSCRIBED)
            self._state = self._ST_OVER
            return

        if self.p.backfill_from is not None:
            self._state = self._ST_FROM
            self._st_start(True)
            self.p.backfill_from.setenvironment(self._env)
            self.p.backfill_from._start()
        else:
            self._start_finish()
            self._state = self._ST_START  # initial state for _load
            self._st_start()

        self._reconns = 0

    def _st_start(self, instart=True):
        if self.p.historical:
            self.put_notification(self.DELAYED)
            dtend = None
            if self.todate < float('inf'):
                dtend = num2date(self.todate)

            dtbegin = None
            if self.fromdate > float('-inf'):
                dtbegin = num2date(self.fromdate, tz=timezone.utc)

            self.qhist = self.o.candles(
                self.p.dataname, dtbegin, dtend,
                self._timeframe, self._compression,
                candleFormat=self._candleFormat,
                includeFirst=True)

            self._state = self._ST_HISTORBACK
            return True

        # depending on candles, either stream or use poll
        if instart:
            self._statelivereconn = self.p.backfill_start
        else:
            self._statelivereconn = self.p.backfill
        if self._statelivereconn:
            self.put_notification(self.DELAYED)
        if not self.p.candles:
            # recreate a new stream on call
            logger.info("Creating a new price stream from oanda servers")
            self.qlive = self.o.streaming_prices(
                self.p.dataname)
        elif instart:
            # poll thread will never die, so no need to recreate it
            self.poll_thread()
        self._state = self._ST_LIVE
        return True  # no return before - implicit continue

    def poll_thread(self):
        t = threading.Thread(target=self._t_poll)
        t.daemon = True
        t.start()

    def _t_poll(self):
        dtstart = self._getstarttime(
            self._timeframe,
            self._compression,
            offset=1)
        while True:
            dtcurr = self._getstarttime(self._timeframe, self._compression)
            # request candles in live instead of stream
            if dtcurr > dtstart:
                if len(self) > 1:
                    # len == 1 ... forwarded for the 1st time
                    dtbegin = self.datetime.datetime(-1)
                elif self.fromdate > float('-inf'):
                    dtbegin = num2date(self.fromdate, tz=timezone.utc)
                else:  # 1st bar and no begin set
                    dtbegin = dtstart
                self.qlive = self.o.candles(
                    self.p.dataname, dtbegin, None,
                    self._timeframe, self._compression,
                    candleFormat=self._candleFormat,
                    onlyComplete=True,
                    includeFirst=False)
                dtstart = dtbegin
                # sleep until next call
                dtnow = datetime.utcnow()
                dtnext = self._getstarttime(
                    self._timeframe,
                    self._compression,
                    dt=dtnow,
                    offset=-1)
                dtdiff = dtnext - dtnow
                tmout = (dtdiff.days * 24 * 60 * 60) + \
                    dtdiff.seconds + self.p.candles_delay
                if tmout <= 0:
                    tmout = self.p.candles_delay
                _time.sleep(tmout)

    def _getstarttime(self, timeframe, compression, dt=None, offset=0):
        '''
        This method will return the start of the period based on current
        time (or provided time).
        '''
        sessionstart = self.p.sessionstart
        if sessionstart is None:
            # use UTC 22:00 (5:00 pm New York) as default
            sessionstart = time(hour=22, minute=0, second=0)
        if dt is None:
            dt = datetime.utcnow()
        if timeframe == TimeFrame.Seconds:
            dt = dt.replace(
                second=(dt.second // compression) * compression,
                microsecond=0)
            if offset:
                dt = dt - timedelta(seconds=compression*offset)
        elif timeframe == TimeFrame.Minutes:
            if compression >= 60:
                hours = 0
                minutes = 0
                # get start of day
                dtstart = self._getstarttime(TimeFrame.Days, 1, dt)
                # diff start of day with current time to get seconds
                # since start of day
                dtdiff = dt - dtstart
                hours = dtdiff.seconds//((60*60)*(compression//60))
                minutes = compression % 60
                dt = dtstart + timedelta(hours=hours, minutes=minutes)
            else:
                dt = dt.replace(
                    minute=(dt.minute // compression) * compression,
                    second=0,
                    microsecond=0)
            if offset:
                dt = dt - timedelta(minutes=compression*offset)
        elif timeframe == TimeFrame.Days:
            if dt.hour < sessionstart.hour:
                dt = dt - timedelta(days=1)
            if offset:
                dt = dt - timedelta(days=offset)
            dt = dt.replace(
                hour=sessionstart.hour,
                minute=sessionstart.minute,
                second=sessionstart.second,
                microsecond=sessionstart.microsecond)
        elif timeframe == TimeFrame.Weeks:
            if dt.weekday() != 6:
                # sunday is start of week at 5pm new york
                dt = dt - timedelta(days=dt.weekday() + 1)
            if offset:
                dt = dt - timedelta(days=offset * 7)
            dt = dt.replace(
                hour=sessionstart.hour,
                minute=sessionstart.minute,
                second=sessionstart.second,
                microsecond=sessionstart.microsecond)
        elif timeframe == TimeFrame.Months:
            if offset:
                dt = dt - timedelta(days=(min(28 + dt.day, 31)))
            # last day of month
            last_day_of_month = dt.replace(day=28) + timedelta(days=4)
            last_day_of_month = last_day_of_month - timedelta(
                days=last_day_of_month.day)
            last_day_of_month = last_day_of_month.day
            # start of month (1 at 0, 22 last day of prev month)
            if dt.day < last_day_of_month:
                dt = dt - timedelta(days=dt.day)
            dt = dt.replace(
                hour=sessionstart.hour,
                minute=sessionstart.minute,
                second=sessionstart.second,
                microsecond=sessionstart.microsecond)
        return dt

    def stop(self):
        '''
        Stops and tells the store to stop
        '''
        super(OandaV20Data, self).stop()
        self.o.stop()

    def replay(self, **kwargs):
        # save original timeframe and compression to fetch data
        # they will be overriden when calling replay
        orig_timeframe = self._timeframe
        orig_compression = self._compression
        # setting up replay configuration
        super(DataBase, self).replay(**kwargs)
        # putting back original timeframe and compression to fetch correct data
        # the replay configuration will still use the correct dataframe and
        # compression for strategy
        self._timeframe = orig_timeframe
        self._compression = orig_compression

    def haslivedata(self):
        return bool(self._storedmsg or self.qlive)  # do not return the objs

    def _load(self):
        if self._state == self._ST_OVER:
            return False

        while True:
            if self._state == self._ST_LIVE:
                try:
                    msg = (self._storedmsg.pop(None, None) or
                           self.qlive.get(timeout=self._qcheck))
                except queue.Empty:
                    return None

                if 'msg' in msg:
                    self.put_notification(self.CONNBROKEN)

                    if not self.p.reconnect or self._reconns == 0:
                        # Can no longer reconnect
                        self.put_notification(self.DISCONNECTED)
                        self._state = self._ST_OVER
                        return False  # failed
                    # sleep only on reconnect
                    if self._reconns != self.p.reconnections:
                        _time.sleep(self.o.p.reconntimeout)
                    # Can reconnect
                    self._reconns -= 1
                    self._st_start(instart=False)
                    continue

                self._reconns = self.p.reconnections

                # Process the message according to expected return type
                if not self._statelivereconn:
                    if self._laststatus != self.LIVE:
                        if self.qlive.qsize() <= 1:  # very short live queue
                            self.put_notification(self.LIVE)
                    if msg:
                        if self.p.candles:
                            ret = self._load_candle(msg)
                        else:
                            ret = self._load_tick(msg)
                        if ret:
                            return True

                    # could not load bar ... go and get new one
                    continue

                # Fall through to processing reconnect - try to backfill
                self._storedmsg[None] = msg  # keep the msg

                # else do a backfill
                if self._laststatus != self.DELAYED:
                    self.put_notification(self.DELAYED)

                dtend = None
                if len(self) > 1:
                    # len == 1 ... forwarded for the 1st time
                    dtbegin = self.datetime.datetime(
                        -1).astimezone(timezone.utc)
                elif self.fromdate > float('-inf'):
                    dtbegin = num2date(self.fromdate, tz=timezone.utc)
                else:  # 1st bar and no begin set
                    # passing None to fetch max possible in 1 request
                    dtbegin = None
                if msg:
                    dtend = datetime.utcfromtimestamp(float(msg['time']))

                # TODO not sure if incomplete candles may destruct something
                self.qhist = self.o.candles(
                    self.p.dataname, dtbegin, dtend,
                    self._timeframe, self._compression,
                    candleFormat=self._candleFormat,
                    includeFirst=True, onlyComplete=False)

                self._state = self._ST_HISTORBACK
                self._statelivereconn = False  # no longer in live
                continue

            elif self._state == self._ST_HISTORBACK:
                msg = self.qhist.get()
                if msg is None:
                    continue

                elif 'msg' in msg:  # Error
                    if not self.p.reconnect or self._reconns == 0:
                        # Can no longer reconnect
                        self.put_notification(self.DISCONNECTED)
                        self._state = self._ST_OVER
                        return False  # failed

                    # Can reconnect
                    self._reconns -= 1
                    self._st_start(instart=False)
                    continue

                if msg:
                    if self._load_candle(msg):
                        return True  # loading worked
                    continue  # not loaded ... date may have been seen
                else:
                    # End of histdata
                    if self.p.historical:  # only historical
                        self.put_notification(self.DISCONNECTED)
                        self._state = self._ST_OVER
                        return False  # end of historical

                # Live is also wished - go for it
                self._state = self._ST_LIVE
                continue

            elif self._state == self._ST_FROM:
                if not self.p.backfill_from.next():
                    # additional data source is consumed
                    self._state = self._ST_START
                    continue

                # copy lines of the same name
                for alias in self.l.getlinealiases():
                    lsrc = getattr(self.p.backfill_from.lines, alias)
                    ldst = getattr(self.lines, alias)

                    ldst[0] = lsrc[0]

                return True

            elif self._state == self._ST_START:
                if not self._st_start(instart=False):
                    self._state = self._ST_OVER
                    return False

    def _load_tick(self, msg):
        if 'time' not in msg:
            logger.error("time field missing from tick message {}".format(msg))
            return False
        if 'asks' not in msg:
            logger.error("asks field missing from tick message {}".format(msg))
            return False
        if 'bids' not in msg:
            logger.error("bids field missing from tick message {}".format(msg))
            return False
        dtobj = datetime.utcfromtimestamp(float(msg['time']))
        dt = date2num(dtobj)
        if dt <= self.l.datetime[-1]:
            return False  # time already seen

        # common fields
        self.l.datetime[0] = dt
        self.l.volume[0] = 0.0
        self.l.openinterest[0] = 0.0

        # put the prices into the bar
        price = {}
        price['ask'] = float(msg['asks'][0]['price'])
        price['bid'] = float(msg['bids'][0]['price'])
        price['mid'] = round(
            (price['bid'] + price['ask']) / 2,
            self.contractdetails['displayPrecision'])
        if self.p.bidask:
            if self.p.useask:
                price[None] = 'ask'
            else:
                price[None] = 'bid'
        else:
            price[None] = 'mid'
        for t in ['open', 'high', 'low', 'close']:
            getattr(self.l, t)[0] = price[price[None]]
        for x in ['mid', 'bid', 'ask']:
            getattr(self.l, f'{x}_close')[0] = price[x]

        self.l.volume[0] = 0.0
        self.l.openinterest[0] = 0.0

        return True

    def _load_candle(self, msg):
        dtobj = datetime.utcfromtimestamp(float(msg['time']))
        if self.p.adjstarttime:
            # move time to start time of next candle
            # and subtract 0.1 miliseconds (ensures no
            # rounding issues, 10 microseconds is minimum)
            dtobj = self._getstarttime(
                self.p.timeframe,
                self.p.compression,
                dtobj,
                -1) - timedelta(microseconds=100)
        dt = date2num(dtobj)
        if dt <= self.l.datetime[-1]:
            return False  # time already seen

        # common fields
        self.l.datetime[0] = dt
        self.l.volume[0] = float(msg['volume'])
        self.l.openinterest[0] = 0.0

        # put the prices into the bar
        price = {'bid': {}, 'ask': {}, 'mid': {}}
        for price_side in price:
            price[price_side]['open'] = msg[price_side]['o']
            price[price_side]['high'] = msg[price_side]['h']
            price[price_side]['low'] = msg[price_side]['l']
            price[price_side]['close'] = msg[price_side]['c']

        # select default price side for ohlc values
        if self.p.bidask:
            if not self.p.useask:
                price[None] = price['bid']
            else:
                price[None] = price['ask']
        else:
            price[None] = price['mid']
        # set ohlc values
        for x in price[None]:
            getattr(self.l, x)[0] = price[None][x]
        # set all close values
        for x in ['mid', 'bid', 'ask']:
            ident = f'{x}_close'
            getattr(self.l, ident)[0] = price[x]['close']

        return True
