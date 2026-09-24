from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import threading
import time


@dataclass
class _FeedJob:
    raw: bytes
    context: dict
    onDone: object
    epoch: int
    pcmMs: float


@dataclass
class _IdleJob:
    done: threading.Event
    epoch: int
    tailRequested: bool
    waitForCompletion: bool


class SpeechFifoMixer:
    """Single-worker FIFO master stream for NVDA speech.

    Source feed() copies PCM into one per-WavePlayer FIFO and returns quickly.
    The worker renders source audio in approximately 20 ms blocks and feeds the
    same original WavePlayer.  TEST55 retains TEST54 generation of Delay/Reverb
    tail blocks in this exact worker before the native idle boundary.

    A tail-enabled idle is intentionally asynchronous, matching the add-on's
    established post-speech tail behaviour: the synth thread is released while
    the worker continues wet-only PCM. If new source PCM arrives while wet audio
    is already queued, enqueue() immediately calls native stop to flush that wet
    backlog, but it does not reset the DSP state. The worker then feeds the new
    dry source through the same Delay/Reverb memories. Explicit stop() invalidates
    both source and tail work and flushes the native player immediately.
    """

    def __init__(
        self,
        player,
        renderChunk,
        nativeFeed,
        nativeIdle,
        nativeStop,
        eventCallback=None,
        beginTail=None,
        renderTail=None,
        blockSeconds=0.020,
    ):
        self.player = player
        self._renderChunk = renderChunk
        self._nativeFeed = nativeFeed
        self._nativeIdle = nativeIdle
        self._nativeStop = nativeStop
        self._eventCallback = eventCallback
        self._beginTail = beginTail
        self._renderTail = renderTail
        self._blockSeconds = max(0.005, float(blockSeconds))
        self._cond = threading.Condition(threading.RLock())
        self._jobs = deque()
        self._epoch = 1
        self._closed = False
        self._queuedPcmMs = 0.0
        self._currentPcmMs = 0.0
        self._tailActive = False
        self._tailCancelRequested = False
        self._thread = threading.Thread(
            target=self._run,
            name='AudioProcessorSpeechFifoMixer',
            daemon=True,
        )
        self._thread.start()

    def _event(self, name, **fields):
        # TEST59 release path: only lifecycle completion and errors are functional.
        # Earlier mixer telemetry events are deliberately discarded here so the
        # 20 ms audio path does not continuously build/report diagnostic records.
        if name not in ('boundaryComplete', 'error'):
            return
        callback = self._eventCallback
        if callback is None:
            return
        try:
            callback(name, **fields)
        except Exception:
            pass

    def queuedPcmMs(self):
        with self._cond:
            return max(0.0, float(self._queuedPcmMs) + float(self._currentPcmMs))

    def tailActive(self):
        with self._cond:
            return bool(self._tailActive)

    def enqueue(self, raw, context, onDone=None):
        raw = bytes(raw)
        ctx = dict(context)
        sourceChannels = max(1, int(ctx.get('sourceChannels', 1)))
        bits = max(8, int(ctx.get('bits', 16)))
        sampleRate = max(1, int(ctx.get('sampleRate', 1)))
        bytesPerFrame = max(1, sourceChannels * max(1, bits // 8))
        pcmMs = (len(raw) / float(bytesPerFrame * sampleRate)) * 1000.0
        flushWet = False
        with self._cond:
            if self._closed:
                return False
            # If the worker is currently producing wet-only continuation, the
            # native player may already own ~200 ms of wet PCM. Real speech must
            # never sit behind that backlog. Flush native output now, preserve
            # the DSP memory, and let the worker switch to the new source job.
            if self._tailActive:
                self._tailCancelRequested = True
                flushWet = True
            epoch = self._epoch
            self._jobs.append(_FeedJob(raw, ctx, onDone, epoch, pcmMs))
            self._queuedPcmMs += pcmMs
            depth = self._queuedPcmMs + self._currentPcmMs
            count = len(self._jobs)
            self._cond.notify_all()
        if flushWet:
            self._event('tailFlushForSource', queueMs=depth)
            try:
                self._nativeStop(self.player)
            except Exception as error:
                self._event('error', error=repr(error))
        self._event('enqueue', bytes=len(raw), pcmMs=pcmMs, queueMs=depth, jobs=count)
        return True

    def idle(self, tailRequested=False):
        tailRequested = bool(tailRequested)
        done = threading.Event()
        waitForCompletion = not tailRequested
        with self._cond:
            if self._closed:
                return False
            epoch = self._epoch
            self._jobs.append(_IdleJob(done, epoch, tailRequested, waitForCompletion))
            self._cond.notify_all()
        self._event('idleQueued', queueMs=self.queuedPcmMs(), tailRequested=tailRequested)
        # Without tails retain TEST53's exact synchronous WavePlayer.idle contract.
        # With tails return immediately, as the previous Gate Hold implementation
        # did, while the same worker owns the continuous wet stream.
        if waitForCompletion:
            done.wait()
        return True

    def stop(self):
        pendingBarriers = []
        with self._cond:
            if self._closed:
                return
            self._epoch += 1
            self._tailCancelRequested = True
            while self._jobs:
                job = self._jobs.popleft()
                if isinstance(job, _IdleJob):
                    pendingBarriers.append(job.done)
            self._queuedPcmMs = 0.0
            self._cond.notify_all()
        for event in pendingBarriers:
            event.set()
        self._event('stop', queueMs=0.0)
        # Native stop also unblocks a worker currently waiting inside native feed.
        self._nativeStop(self.player)

    def close(self):
        pendingBarriers = []
        with self._cond:
            if self._closed:
                return
            self._closed = True
            self._epoch += 1
            self._tailCancelRequested = True
            while self._jobs:
                job = self._jobs.popleft()
                if isinstance(job, _IdleJob):
                    pendingBarriers.append(job.done)
            self._queuedPcmMs = 0.0
            self._cond.notify_all()
        for event in pendingBarriers:
            event.set()
        try:
            self._nativeStop(self.player)
        except Exception:
            pass

    def _jobCurrent(self, epoch):
        with self._cond:
            return (not self._closed) and int(epoch) == int(self._epoch)

    def _consumeCurrentMs(self, amount):
        with self._cond:
            self._currentPcmMs = max(0.0, self._currentPcmMs - max(0.0, float(amount)))

    def _pendingSourceAfterBoundary(self):
        with self._cond:
            return any(isinstance(job, _FeedJob) for job in self._jobs)

    def _tailShouldStop(self, epoch):
        with self._cond:
            return (
                self._closed
                or int(epoch) != int(self._epoch)
                or self._tailCancelRequested
                or any(isinstance(job, _FeedJob) for job in self._jobs)
            )

    def _runFeedJob(self, job):
        ctx = job.context
        sourceChannels = max(1, int(ctx.get('sourceChannels', 1)))
        bits = max(8, int(ctx.get('bits', 16)))
        sampleRate = max(1, int(ctx.get('sampleRate', 1)))
        bytesPerFrame = max(1, sourceChannels * max(1, bits // 8))
        blockFrames = max(1, int(round(sampleRate * self._blockSeconds)))
        blockBytes = max(bytesPerFrame, blockFrames * bytesPerFrame)
        usable = len(job.raw) - (len(job.raw) % bytesPerFrame)
        raw = job.raw[:usable]

        if not raw:
            if not self._jobCurrent(job.epoch):
                return
            t0 = time.perf_counter()
            self._nativeFeed(self.player, b'', None, job.onDone)
            t1 = time.perf_counter()
            self._event('chunk', sourceBytes=0, sourcePcmMs=0.0, dspMs=0.0,
                        nativeFeedMs=(t1-t0)*1000.0, final=True, context=ctx)
            return

        offset = 0
        total = len(raw)
        while offset < total:
            if not self._jobCurrent(job.epoch):
                return
            end = min(total, offset + blockBytes)
            piece = raw[offset:end]
            final = end >= total
            sourcePcmMs = (len(piece) / float(bytesPerFrame * sampleRate)) * 1000.0
            renderStart = time.perf_counter()
            processed = self._renderChunk(self.player, piece, ctx)
            renderEnd = time.perf_counter()
            if not self._jobCurrent(job.epoch):
                return
            callback = job.onDone if final else None
            nativeStart = time.perf_counter()
            self._nativeFeed(self.player, processed, None, callback)
            nativeEnd = time.perf_counter()
            self._consumeCurrentMs(sourcePcmMs)
            self._event(
                'chunk',
                sourceBytes=len(piece),
                sourcePcmMs=sourcePcmMs,
                dspMs=(renderEnd-renderStart)*1000.0,
                nativeFeedMs=(nativeEnd-nativeStart)*1000.0,
                final=bool(final),
                context=ctx,
            )
            offset = end

    def _runTail(self, job):
        if not job.tailRequested or self._beginTail is None or self._renderTail is None:
            return 'none'
        # If a later source feed is already queued, this idle boundary has already
        # been superseded. Do not insert a tail or a native idle between utterances.
        if self._pendingSourceAfterBoundary():
            self._event('tailSuperseded', reason='source-already-queued')
            return 'superseded'
        try:
            plan = self._beginTail(self.player)
        except Exception as error:
            self._event('error', error=repr(error))
            return 'none'
        if not plan:
            return 'none'

        with self._cond:
            if self._closed or int(job.epoch) != int(self._epoch):
                return 'cancelled'
            self._tailActive = True
            self._tailCancelRequested = False
        self._event('tailStart')
        renderedMs = 0.0
        try:
            while not self._tailShouldStop(job.epoch):
                renderStart = time.perf_counter()
                result = self._renderTail(self.player, plan)
                renderEnd = time.perf_counter()
                if not result:
                    break
                data, pcmMs, done, peakRatio = result
                if not data:
                    break
                if self._tailShouldStop(job.epoch):
                    return 'superseded' if self._pendingSourceAfterBoundary() else 'cancelled'
                nativeStart = time.perf_counter()
                self._nativeFeed(self.player, data, None, None)
                nativeEnd = time.perf_counter()
                renderedMs += max(0.0, float(pcmMs))
                self._event(
                    'tailChunk', pcmMs=float(pcmMs), peakRatio=float(peakRatio),
                    dspMs=(renderEnd-renderStart)*1000.0,
                    nativeFeedMs=(nativeEnd-nativeStart)*1000.0,
                    renderedMs=renderedMs,
                )
                if done:
                    return 'complete'
            if self._pendingSourceAfterBoundary():
                return 'superseded'
            return 'cancelled' if not self._jobCurrent(job.epoch) else 'complete'
        finally:
            # Keep _tailActive true through the following native idle/sync drain.
            # A newly arriving source must be able to call native stop and flush
            # the final queued wet reserve even after the last tail block was rendered.
            pass

    def _clearTailBoundaryFlag(self, epoch):
        with self._cond:
            self._tailActive = False
            if not self._closed and int(epoch) == int(self._epoch):
                self._tailCancelRequested = False
            self._cond.notify_all()

    def _finishIdleBoundary(self, job):
        status = self._runTail(job)
        if status in ('superseded', 'cancelled'):
            self._clearTailBoundaryFlag(job.epoch)
            self._event('tailInterrupted', status=status)
            return False
        if not self._jobCurrent(job.epoch):
            self._clearTailBoundaryFlag(job.epoch)
            return False
        if job.tailRequested and self._pendingSourceAfterBoundary():
            self._clearTailBoundaryFlag(job.epoch)
            self._event('tailInterrupted', status='source-before-idle')
            return False
        try:
            start = time.perf_counter()
            self._nativeIdle(self.player)
            end = time.perf_counter()
            # enqueue() can interrupt this sync while _tailActive remains true.
            if not self._jobCurrent(job.epoch):
                return False
            with self._cond:
                interrupted = self._tailCancelRequested or any(isinstance(x, _FeedJob) for x in self._jobs)
            if interrupted:
                self._event('tailInterrupted', status='source-during-idle-drain')
                return False
            self._event('idleNative', nativeIdleMs=(end-start)*1000.0, tailStatus=status)
            self._event('boundaryComplete', tailStatus=status)
            return True
        finally:
            self._clearTailBoundaryFlag(job.epoch)

    def _run(self):
        while True:
            with self._cond:
                while not self._jobs and not self._closed:
                    self._cond.wait()
                if self._closed:
                    return
                job = self._jobs.popleft()
                if isinstance(job, _FeedJob):
                    self._queuedPcmMs = max(0.0, self._queuedPcmMs - job.pcmMs)
                    self._currentPcmMs = max(0.0, job.pcmMs)
            if isinstance(job, _FeedJob):
                try:
                    self._runFeedJob(job)
                except Exception as error:
                    self._event('error', error=repr(error))
                    try:
                        self._nativeStop(self.player)
                    except Exception:
                        pass
                finally:
                    with self._cond:
                        self._currentPcmMs = 0.0
                continue

            try:
                self._finishIdleBoundary(job)
            except Exception as error:
                self._event('error', error=repr(error))
            finally:
                job.done.set()
