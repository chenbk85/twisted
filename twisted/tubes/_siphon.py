# -*- test-case-name: twisted.tubes.test.test_tube -*-

"""
Adapters for converting L{ITube} to L{IDrain} and L{IFount}.
"""

import itertools

from zope.interface import implementer

from .itube import IPause, IDrain, IFount, ITube
from .pauser import Pauser
from ._components import _registryAdapting

from twisted.python.failure import Failure
from twisted.internet.defer import Deferred

from twisted.python import log

class _SiphonPiece(object):
    """
    Shared functionality between L{_SiphonFount} and L{_SiphonDrain}
    """
    def __init__(self, siphon):
        self._siphon = siphon


    @property
    def _tube(self):
        return self._siphon._tube



@implementer(IFount)
class _SiphonFount(_SiphonPiece):
    """
    Implementation of L{IFount} for L{_Siphon}.

    @ivar fount: the implementation of the L{IDrain.fount} attribute.  The
        L{IFount} which is flowing to this L{_Siphon}'s L{IDrain}
        implementation.

    @ivar drain: the implementation of the L{IFount.drain} attribute.  The
        L{IDrain} to which this L{_Siphon}'s L{IFount} implementation is
        flowing.
    """
    drain = None

    def __init__(self, siphon):
        super(_SiphonFount, self).__init__(siphon)
        self._pauser = Pauser(self._actuallyPause, self._actuallyResume)


    def __repr__(self):
        """
        Nice string representation.
        """
        return "<Fount for {0}>".format(repr(self._siphon._tube))


    @property
    def outputType(self):
        return self._tube.outputType


    def flowTo(self, drain):
        """
        Flow data from this L{_Siphon} to the given drain.
        """
        if self.drain:
            # FIXME: direct test for this.  The behavior here ought to be that
            # when we make it so that our drain is no longer actually our
            # drain, it stops telling us to pause/stop/etc.  Open question:
            # what if it had already paused us?  Can we simply discard that
            # now?  Note that this flowingFrom may re-entrantly call this
            # flowTo again, which is probably nonsense, but ugh, what should
            # even happen then...
            self.drain.flowingFrom(None)
        self.drain = drain
        if drain is None:
            return
        result = self.drain.flowingFrom(self)
        if self._siphon._pauseBecauseNoDrain:
            pbnd = self._siphon._pauseBecauseNoDrain
            self._siphon._pauseBecauseNoDrain = None
            pbnd.unpause()
        self._siphon._unbufferIterator()
        return result


    def pauseFlow(self):
        """
        Pause the flow from the fount, or remember to do that when the
        fount is attached, if it isn't yet.
        """
        return self._pauser.pause()


    def _actuallyPause(self):
        fount = self._siphon._tdrain.fount
        self._siphon._currentlyPaused = True
        if fount is not None and self._siphon._pauseBecausePauseCalled is None:
            self._siphon._pauseBecausePauseCalled = fount.pauseFlow()


    def _actuallyResume(self):
        """
        Resume the flow from the fount to this L{_Siphon}.
        """
        self._siphon._currentlyPaused = False

        self._siphon._unbufferIterator()
        if self._siphon._currentlyPaused:
            return

        if self._siphon._pauseBecausePauseCalled:
            # TODO: validate that the siphon's fount is always set consisetntly
            # with _pauseBecausePauseCalled.
            fp = self._siphon._pauseBecausePauseCalled
            self._siphon._pauseBecausePauseCalled = None
            fp.unpause()


    def stopFlow(self):
        """
        Stop the flow from the fount to this L{_Siphon}.
        """
        self._siphon._flowWasStopped = True
        fount = self._siphon._tdrain.fount
        if fount is None:
            return
        fount.stopFlow()



@implementer(IPause)
class _PlaceholderPause(object):

    def unpause(self):
        """
        No-op.
        """



@implementer(IDrain)
class _SiphonDrain(_SiphonPiece):
    """
    Implementation of L{IDrain} for L{_Siphon}.
    """
    fount = None

    def __repr__(self):
        """
        Nice string representation.
        """
        return '<Drain for {0}>'.format(self._siphon._tube)


    @property
    def inputType(self):
        return self._tube.inputType


    def flowingFrom(self, fount):
        """
        This siphon will now have 'receive' called.
        """
        if fount is not None:
            out = fount.outputType
            in_ = self.inputType
            if out is not None and in_ is not None:
                if not in_.isOrExtends(out):
                    raise TypeError()
        self.fount = fount
        if self._siphon._pauseBecausePauseCalled:
            pbpc = self._siphon._pauseBecausePauseCalled
            self._siphon._pauseBecausePauseCalled = None
            pbpc.unpause()
            if fount is None:
                pauseFlow = _PlaceholderPause
            else:
                pauseFlow = fount.pauseFlow
            self._siphon._pauseBecausePauseCalled = pauseFlow()
        if fount is not None:
            if self._siphon._flowWasStopped:
                fount.stopFlow()
            # Is this the right place, or does this need to come after
            # _pauseBecausePauseCalled's check?
            if not self._siphon._everStarted:
                self._siphon._everStarted = True
                self._siphon._deliverFrom(self._tube.started)
        nextFount = self._siphon._tfount
        nextDrain = nextFount.drain
        if nextDrain is None:
            return nextFount
        return nextFount.flowTo(nextDrain)


    def receive(self, item):
        """
        An item was received.  Pass it on to the tube for processing.
        """
        def thingToDeliverFrom():
            return self._tube.received(item)
        self._siphon._deliverFrom(thingToDeliverFrom)


    def flowStopped(self, reason):
        """
        This siphon has now stopped.
        """
        self._siphon._flowStoppingReason = reason
        self._siphon._deliverFrom(lambda: self._tube.stopped(reason))



class _Siphon(object):
    """
    A L{_Siphon} is an L{IDrain} and possibly also an L{IFount}, and provides
    lots of conveniences to make it easy to implement something that does fancy
    flow control with just a few methods.

    @ivar _tube: the L{Tube} which will receive values from this siphon and
        call C{deliver} to deliver output to it.  (When set, this will
        automatically set the C{siphon} attribute of said L{Tube} as well, as
        well as un-setting the C{siphon} attribute of the old tube.)

    @ivar _currentlyPaused: is this L{_Siphon} currently paused?  Boolean:
        C{True} if paused, C{False} if not.

    @ivar _pauseBecausePauseCalled: an L{IPause} from the upstream fount,
        present because pauseFlow has been called.

    @ivar _flowStoppingReason: If this is not C{None}, then call C{flowStopped}
        on the downstream L{IDrain} at the next opportunity, where "the next
        opportunity" is when the last L{Deferred} yielded from L{ITube.stopped}
        has fired.

    @ivar _everStarted: Has this L{_Siphon} ever called C{started} on its
        L{Tube}?
    @type _everStarted: L{bool}
    """

    _currentlyPaused = False
    _pauseBecausePauseCalled = None
    _tube = None
    _pendingIterator = None
    _flowWasStopped = False
    _everStarted = False
    _unbuffering = False
    _flowStoppingReason = None
    _pauseBecauseNoDrain = None

    def __init__(self, tube):
        """
        Initialize this L{_Siphon} with the given L{Tube} to control its
        behavior.
        """
        self._tfount = _SiphonFount(self)
        self._tdrain = _SiphonDrain(self)
        self._tube = tube


    def __repr__(self):
        """
        Nice string representation.
        """
        return '<_Siphon for {0}>'.format(repr(self._tube))


    def _deliverFrom(self, deliverySource):
        assert self._pendingIterator is None, \
            repr(list(self._pendingIterator)) + " " + \
            repr(deliverySource) + " " + \
            repr(self._pauseBecauseNoDrain)
        try:
            iterableOrNot = deliverySource()
        except:
            f = Failure()
            log.err(f, "Exception raised when delivering from {0!r}"
                    .format(deliverySource))
            self._tdrain.fount.stopFlow()
            downstream = self._tfount.drain
            if downstream is not None:
                downstream.flowStopped(f)
            return
        if iterableOrNot is None:
            return 0
        self._pendingIterator = iter(iterableOrNot)
        if self._tfount.drain is None:
            if self._pauseBecauseNoDrain is None:
                self._pauseBecauseNoDrain = self._tfount.pauseFlow()

        self._unbufferIterator()


    def _unbufferIterator(self):
        if self._unbuffering:
            return
        if self._pendingIterator is None:
            return
        whatever = object()
        self._unbuffering = True
        while not self._currentlyPaused:
            value = next(self._pendingIterator, whatever)
            if value is whatever:
                self._pendingIterator = None
                if self._flowStoppingReason is not None:
                    self._tfount.drain.flowStopped(self._flowStoppingReason)
                break
            if isinstance(value, Deferred):
                anPause = self._tfount.pauseFlow()

                def whenUnclogged(result):
                    pending = self._pendingIterator
                    self._pendingIterator = itertools.chain(iter([result]),
                                                            pending)
                    anPause.unpause()
                value.addCallback(whenUnclogged).addErrback(log.err, "WHAT")
            else:
                self._tfount.drain.receive(value)
        self._unbuffering = False



def _tube2drain(tube):
    return _Siphon(tube)._tdrain



_tubeRegistry = _registryAdapting(
    (ITube, IDrain, _tube2drain),
)


