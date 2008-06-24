# Written by Bram Cohen and Pawel Garbacki
# see LICENSE.txt for license information

from Tribler.Core.BitTornado.CurrentRateMeasure import Measure
from Tribler.Core.BitTornado.bitfield import Bitfield
from random import shuffle
from Tribler.Core.BitTornado.clock import clock
# 2fastbt_
from Tribler.Core.CoopDownload.Helper import SingleDownloadHelperInterface
from traceback import print_stack
# _2fastbt

import sys
import time

try:
    True
except:
    True = 1
    False = 0

DEBUG = False
EXPIRE_TIME = 60 * 60

class PerIPStats:  
    def __init__(self, ip):
        self.numgood = 0
        self.bad = {}
        self.numconnections = 0
        self.lastdownload = None
        self.peerid = None

class BadDataGuard:
    def __init__(self, download):
        self.download = download
        self.ip = download.ip
        self.downloader = download.downloader
        self.stats = self.downloader.perip[self.ip]
        self.lastindex = None

    def failed(self, index, bump = False):
        self.stats.bad.setdefault(index, 0)
        self.downloader.gotbaddata[self.ip] = 1
        self.stats.bad[index] += 1
        if len(self.stats.bad) > 1:
            if self.download is not None:
                self.downloader.try_kick(self.download)
            elif self.stats.numconnections == 1 and self.stats.lastdownload is not None:
                self.downloader.try_kick(self.stats.lastdownload)
        if len(self.stats.bad) >= 3 and len(self.stats.bad) > int(self.stats.numgood/30):
            self.downloader.try_ban(self.ip)
        elif bump:
            self.downloader.picker.bump(index)

    def good(self, index):
        # lastindex is a hack to only increase numgood by one for each good
        # piece, however many chunks come from the connection(s) from this IP
        if index != self.lastindex:
            self.stats.numgood += 1
            self.lastindex = index

# 2fastbt_
class SingleDownload(SingleDownloadHelperInterface):
# _2fastbt
    def __init__(self, downloader, connection):
# 2fastbt_
        SingleDownloadHelperInterface.__init__(self)
# _2fastbt
        self.downloader = downloader
        self.connection = connection
        self.choked = True
        self.interested = False
        self.active_requests = []
        self.measure = Measure(downloader.max_rate_period)
        self.peermeasure = Measure(downloader.max_rate_period)
        self.have = Bitfield(downloader.numpieces)
        self.last = -1000
        self.last2 = -1000
        self.example_interest = None
        self.backlog = 2
        self.ip = connection.get_ip()
        self.guard = BadDataGuard(self)
# 2fastbt_
        self.helper = downloader.picker.helper
# _2fastbt

    def _backlog(self, just_unchoked):
        self.backlog = int(min(
            2+int(4*self.measure.get_rate()/self.downloader.chunksize),
            (2*just_unchoked)+self.downloader.queue_limit() ))
        if self.backlog > 50:
            self.backlog = int(max(50, self.backlog * 0.075))
        return self.backlog
    
    def disconnected(self):
        self.downloader.lost_peer(self)

        """ JD: obsoleted -- moved to picker.lost_peer

        if self.have.complete():
            self.downloader.picker.lost_seed()
        else:
            for i in xrange(len(self.have)):
                if self.have[i]:
                    self.downloader.picker.lost_have(i)
        """

        if self.have.complete() and self.downloader.storage.is_endgame():
            self.downloader.add_disconnected_seed(self.connection.get_readable_id())
        self._letgo()
        self.guard.download = None

    def _letgo(self):
        if self.downloader.queued_out.has_key(self):
            del self.downloader.queued_out[self]
        if not self.active_requests:
            return
        if self.downloader.endgamemode:
            self.active_requests = []
            return
        lost = {}
        for index, begin, length in self.active_requests:
            self.downloader.storage.request_lost(index, begin, length)
            lost[index] = 1
        lost = lost.keys()
        self.active_requests = []
        if self.downloader.paused:
            return
        ds = [d for d in self.downloader.downloads if not d.choked]
        shuffle(ds)
        for d in ds:
            d._request_more()
        for d in self.downloader.downloads:
            if d.choked and not d.interested:
                for l in lost:
                    if d.have[l] and self.downloader.storage.do_I_have_requests(l):
                        d.send_interested()
                        break

    def got_choke(self):
        if not self.choked:
            self.choked = True
            self._letgo()

    def got_unchoke(self):
        if self.choked:
            self.choked = False
            if self.interested:
                self._request_more(new_unchoke = True)
            self.last2 = clock()

    def is_choked(self):
        return self.choked

    def is_interested(self):
        return self.interested

    def send_interested(self):
        if not self.interested:
            self.interested = True
            self.connection.send_interested()

    def send_not_interested(self):
        if self.interested:
            self.interested = False
            self.connection.send_not_interested()

    def got_piece(self, index, begin, hashlist, piece):
        """ Returns True if the piece is complete. """

        length = len(piece)
        #if DEBUG:
        #    print >> sys.stderr, 'Downloader: got piece of length %d' % length
        try:
            self.active_requests.remove((index, begin, length))
        except ValueError:
            self.downloader.discarded += length
            return False
        if self.downloader.endgamemode:
            self.downloader.all_requests.remove((index, begin, length))

        self.last = clock()
        self.last2 = clock()
        self.measure.update_rate(length)
        self.downloader.measurefunc(length)
        if not self.downloader.storage.piece_came_in(index, begin, hashlist, piece, self.guard):
            self.downloader.piece_flunked(index)
            return False

        if self.downloader.storage.do_I_have(index):
            self.downloader.picker.complete(index)

        if self.downloader.endgamemode:
            for d in self.downloader.downloads:
                if d is not self:
                    if d.interested:
                        if d.choked:
                            assert not d.active_requests
                            d.fix_download_endgame()
                        else:
                            try:
                                d.active_requests.remove((index, begin, length))
                            except ValueError:
                                continue
                            d.connection.send_cancel(index, begin, length)
                            d.fix_download_endgame()
                    else:
                        assert not d.active_requests
        self._request_more()
        self.downloader.check_complete(index)
        
        # BarterCast counter
        self.connection.total_downloaded += length
    
        return self.downloader.storage.do_I_have(index)

# 2fastbt_
    def helper_forces_unchoke(self):
        self.choked = False
# _2fastbt

    def _request_more(self, new_unchoke = False, slowpieces = []):
# 2fastbt_
        if DEBUG:
            print >>sys.stderr,"Downloader: _request_more()"
        if self.is_frozen_by_helper():
            if DEBUG:
                print >>sys.stderr,"Downloader: _request_more: blocked, returning"
            return
# _2fastbt    
        if self.choked:
            if DEBUG:
                print >>sys.stderr,"Downloader: _request_more: choked, returning"
            return
# 2fastbt_
        # do not download from coordinator
        if self.connection.connection.is_coordinator_con():
            if DEBUG:
                print >>sys.stderr,"Downloader: _request_more: coordinator conn"
            return
# _2fastbt
        if self.downloader.endgamemode:
            self.fix_download_endgame(new_unchoke)
            if DEBUG:
                print >>sys.stderr,"Downloader: _request_more: endgame mode, returning"
            return
        if self.downloader.paused:
            if DEBUG:
                print >>sys.stderr,"Downloader: _request_more: paused, returning"
            return
        if len(self.active_requests) >= self._backlog(new_unchoke):
            if DEBUG:
                print >>sys.stderr,"Downloader: more req than unchoke (active req: %d >= backlog: %d)" % (len(self.active_requests), self._backlog(new_unchoke))
            # Jelle: Schedule _request more to be called in some time. Otherwise requesting and receiving packages
            # may stop, if they arrive to quickly
            if self.downloader.download_rate:
                wait_period = self.downloader.chunksize / self.downloader.download_rate / 2.0
                if DEBUG:
                    print >>sys.stderr,"Downloader: waiting for %f s to call _request_more again" % wait_period
                self.downloader.scheduler(self._request_more, wait_period)
                                          
            if not (self.active_requests or self.backlog):
                self.downloader.queued_out[self] = 1
            return
        
        #if DEBUG:
        #    print >>sys.stderr,"Downloader: _request_more: len act",len(self.active_requests),"back",self.backlog
        
        lost_interests = []
        while len(self.active_requests) < self.backlog:
            #if DEBUG:
            #    print >>sys.stderr,"Downloader: Looking for interesting piece"
            #st = time.time()
            interest = self.downloader.picker.next(self.have,
                               self.downloader.storage.do_I_have_requests,
                               self,
                               self.downloader.too_many_partials(),
                               self.connection.connection.is_helper_con(),
                               slowpieces = slowpieces, connection = self.connection)
            #et = time.time()
            #diff = et-st
            diff=-1
            if DEBUG:
                print >>sys.stderr,"Downloader: _request_more: next() returned",interest,"took %.5f" % (diff)                               
            if interest is None:
                break
            self.example_interest = interest
            self.send_interested()
            loop = True
            while len(self.active_requests) < self.backlog and loop:
                
                begin, length = self.downloader.storage.new_request(interest)
                
                if DEBUG:
                    print >>sys.stderr,"Downloader: new_request",interest,begin,length,"to",self.connection.connection.get_ip(),self.connection.connection.get_port()
                
                self.downloader.picker.requested(interest)
                self.active_requests.append((interest, begin, length))
                self.connection.send_request(interest, begin, length)
                self.downloader.chunk_requested(length)
                if not self.downloader.storage.do_I_have_requests(interest):
                    loop = False
                    lost_interests.append(interest)
        if not self.active_requests:
            self.send_not_interested()
        if lost_interests:
            for d in self.downloader.downloads:
                if d.active_requests or not d.interested:
                    continue
                if d.example_interest is not None and self.downloader.storage.do_I_have_requests(d.example_interest):
                    continue
                for lost in lost_interests:
                    if d.have[lost]:
                        break
                else:
                    continue
# 2fastbt_
                #st = time.time()
                interest = self.downloader.picker.next(d.have,
                                   self.downloader.storage.do_I_have_requests,
                                   self, # Arno, 2008-05-22; self -> d? Original Pawel code
                                   self.downloader.too_many_partials(),
                                   self.connection.connection.is_helper_con(), willrequest=False,connection=self.connection)
                #et = time.time()
                #diff = et-st
                diff=-1
                if DEBUG:                                   
                    print >>sys.stderr,"Downloader: _request_more: next()2 returned",interest,"took %.5f" % (diff)
# _2fastbt
                if interest is None:
                    d.send_not_interested()
                else:
                    d.example_interest = interest
                    
        # Arno: LIVEWRAP: no endgame
        if self.downloader.storage.is_endgame() and not self.downloader.picker.live_streaming:
            self.downloader.start_endgame()


    def fix_download_endgame(self, new_unchoke = False):
# 2fastbt_
        # do not download from coordinator
        if self.downloader.paused or self.connection.connection.is_coordinator_con():
# _2fastbt
            return
        if len(self.active_requests) >= self._backlog(new_unchoke):
            if not (self.active_requests or self.backlog) and not self.choked:
                self.downloader.queued_out[self] = 1
            return
# 2fastbt_
        want = [a for a in self.downloader.all_requests if self.have[a[0]] and a not in self.active_requests and (self.helper is None or self.connection.connection.is_helper_con() or not self.helper.is_ignored(a[0]))]
# _2fastbt
        if not (self.active_requests or want):
            self.send_not_interested()
            return
        if want:
            self.send_interested()
        if self.choked:
            return
        shuffle(want)
        del want[self.backlog - len(self.active_requests):]
        self.active_requests.extend(want)
        for piece, begin, length in want:
# 2fastbt_
            if self.helper is None or self.connection.connection.is_helper_con() or self.helper.reserve_piece(piece,self):
                self.connection.send_request(piece, begin, length)
                self.downloader.chunk_requested(length)
# _2fastbt

    def got_have(self, index):
        if DEBUG:
            print >>sys.stderr,"Downloader: got_have",index
        if index == self.downloader.numpieces-1:
            self.downloader.totalmeasure.update_rate(self.downloader.storage.total_length-(self.downloader.numpieces-1)*self.downloader.storage.piece_length)
            self.peermeasure.update_rate(self.downloader.storage.total_length-(self.downloader.numpieces-1)*self.downloader.storage.piece_length)
        else:
            self.downloader.totalmeasure.update_rate(self.downloader.storage.piece_length)
            self.peermeasure.update_rate(self.downloader.storage.piece_length)

        # Arno: LIVEWRAP
        if not self.downloader.picker.is_valid_piece(index):
            if DEBUG:
                print >>sys.stderr,"Downloader: got_have",index,"is invalid piece"
            return # TODO: should we request_more()? 
        
        if self.have[index]:
            return
        self.have[index] = True
        self.downloader.picker.got_have(index,self.connection)
        if self.have.complete():
            self.downloader.picker.became_seed()
            if self.downloader.picker.am_I_complete():
                self.downloader.add_disconnected_seed(self.connection.get_readable_id())
                self.connection.close()
                return
        if self.downloader.endgamemode:
            self.fix_download_endgame()
        elif ( not self.downloader.paused
               and not self.downloader.picker.is_blocked(index)
               and self.downloader.storage.do_I_have_requests(index) ):
            if not self.choked:
                self._request_more()
            else:
                self.send_interested()

    def _check_interests(self):
        if self.interested or self.downloader.paused:
            return
        for i in xrange(len(self.have)):
            if ( self.have[i] and not self.downloader.picker.is_blocked(i)
                 and ( self.downloader.endgamemode
                       or self.downloader.storage.do_I_have_requests(i) ) ):
                self.send_interested()
                return

    def got_have_bitfield(self, have):
        
        if self.downloader.picker.am_I_complete() and have.complete():
            # Arno: If we're both seeds
            if self.downloader.super_seeding:
                self.connection.send_bitfield(have.tostring()) # be nice, show you're a seed too
            self.connection.close()
            self.downloader.add_disconnected_seed(self.connection.get_readable_id())
            return

        #print >>sys.stderr,"Downloader: got_have_bitfield: VVV#############################################################################################VVVVVVVVVVVVVVVVVVVVVVVVV valid",self.downloader.picker.get_valid_range_iterator(),"len",self.downloader.numpieces
        #print >>sys.stderr,"Downloader: got_have_bitfield: input",`have.toboollist()`
        if have.complete():
            # Arno: He is seed
            self.downloader.picker.got_seed()
        else:
            # Arno: LIVEWRAP: filter out valid pieces
            # TODO: may be slow with 32K pieces.
            validhave = Bitfield(self.downloader.numpieces)
            for i in self.downloader.picker.get_valid_range_iterator():
                if have[i]:
                    validhave[i] = True
                    self.downloader.picker.got_have(i,self.connection)
            have = validhave
        # Store filtered bitfield
        self.have = have
        
        #print >>sys.stderr,"Downloader: got_have_bitfield: valid",`have.toboollist()`
                    
        if self.downloader.endgamemode and not self.downloader.paused:
            for piece, begin, length in self.downloader.all_requests:
                if self.have[piece]:
                    self.send_interested()
                    break
            return
        self._check_interests()

    def get_rate(self):
        return self.measure.get_rate()

    def is_snubbed(self):
# 2fastbt_
        if not self.choked and clock() - self.last2 > self.downloader.snub_time and \
            not self.connection.connection.is_helper_con() and \
            not self.connection.connection.is_coordinator_con():
# _2fastbt
            for index, begin, length in self.active_requests:
                self.connection.send_cancel(index, begin, length)
            self.got_choke()    # treat it just like a choke
        return clock() - self.last > self.downloader.snub_time

    def peer_is_complete(self):
        return self.have.complete()

class Downloader:
    def __init__(self, storage, picker, backlog, max_rate_period,
                 numpieces, chunksize, measurefunc, snub_time,
                 kickbans_ok, kickfunc, banfunc, scheduler = None):
        self.storage = storage
        self.picker = picker
        self.backlog = backlog
        self.max_rate_period = max_rate_period
        self.measurefunc = measurefunc
        self.totalmeasure = Measure(max_rate_period*storage.piece_length/storage.request_size)
        self.numpieces = numpieces
        self.chunksize = chunksize
        self.snub_time = snub_time
        self.kickfunc = kickfunc
        self.banfunc = banfunc
        self.disconnectedseeds = {}
        self.downloads = []
        self.perip = {}
        self.gotbaddata = {}
        self.kicked = {}
        self.banned = {}
        self.kickbans_ok = kickbans_ok
        self.kickbans_halted = False
        self.super_seeding = False
        self.endgamemode = False
        self.endgame_queued_pieces = []
        self.all_requests = []
        self.discarded = 0L
#        self.download_rate = 25000  # 25K/s test rate
        self.download_rate = 0
        self.bytes_requested = 0
        self.last_time = clock()
        self.queued_out = {}
        self.requeueing = False
        self.paused = False
        self.scheduler = scheduler

    def set_download_rate(self, rate):
        self.download_rate = rate * 1000
        self.bytes_requested = 0
        
    def queue_limit(self):
        if not self.download_rate:
            return 10e10    # that's a big queue!
        t = clock()
        self.bytes_requested -= (t - self.last_time) * self.download_rate
        self.last_time = t
        if not self.requeueing and self.queued_out and self.bytes_requested < 0:
            self.requeueing = True
            q = self.queued_out.keys()
            shuffle(q)
            self.queued_out = {}
            for d in q:
                d._request_more()
            self.requeueing = False
        if -self.bytes_requested > 5*self.download_rate:
            self.bytes_requested = -5*self.download_rate
        ql = max(int(-self.bytes_requested/self.chunksize), 0)
        if DEBUG:
            print >> sys.stderr, 'Downloader: download_rate: %s, bytes_requested: %s, chunk: %s -> queue limit: %d' % \
                (self.download_rate, self.bytes_requested, self.chunksize, ql)
        return ql

    def chunk_requested(self, size):
        self.bytes_requested += size

    external_data_received = chunk_requested

    def make_download(self, connection):
        ip = connection.get_ip()
        if self.perip.has_key(ip):
            perip = self.perip[ip]
        else:
            perip = self.perip.setdefault(ip, PerIPStats(ip))
        perip.peerid = connection.get_readable_id()
        perip.numconnections += 1
        d = SingleDownload(self, connection)
        perip.lastdownload = d
        self.downloads.append(d)
        return d

    def piece_flunked(self, index):
        if self.paused:
            return
        if self.endgamemode:
            if self.downloads:
                while self.storage.do_I_have_requests(index):
                    nb, nl = self.storage.new_request(index)
                    self.all_requests.append((index, nb, nl))
                for d in self.downloads:
                    d.fix_download_endgame()
                return
            self._reset_endgame()
            return
        ds = [d for d in self.downloads if not d.choked]
        shuffle(ds)
        for d in ds:
            d._request_more()
        ds = [d for d in self.downloads if not d.interested and d.have[index]]
        for d in ds:
            d.example_interest = index
            d.send_interested()

    def has_downloaders(self):
        return len(self.downloads)

    def lost_peer(self, download):
        ip = download.ip
        self.perip[ip].numconnections -= 1
        if self.perip[ip].lastdownload == download:
            self.perip[ip].lastdownload = None
        self.downloads.remove(download)
        if self.endgamemode and not self.downloads: # all peers gone
            self._reset_endgame()

    def _reset_endgame(self):            
        self.storage.reset_endgame(self.all_requests)
        self.endgamemode = False
        self.all_requests = []
        self.endgame_queued_pieces = []


    def add_disconnected_seed(self, id):
#        if not self.disconnectedseeds.has_key(id):
#            self.picker.seed_seen_recently()
        self.disconnectedseeds[id]=clock()

#   def expire_disconnected_seeds(self):

    def num_disconnected_seeds(self):
        # first expire old ones
        expired = []
        for id, t in self.disconnectedseeds.items():
            if clock() - t > EXPIRE_TIME:     #Expire old seeds after so long
                expired.append(id)
        for id in expired:
#            self.picker.seed_disappeared()
            del self.disconnectedseeds[id]
        return len(self.disconnectedseeds)
        # if this isn't called by a stats-gathering function
        # it should be scheduled to run every minute or two.

    def _check_kicks_ok(self):
        if len(self.gotbaddata) > 10:
            self.kickbans_ok = False
            self.kickbans_halted = True
        return self.kickbans_ok and len(self.downloads) > 2

    def try_kick(self, download):
        if self._check_kicks_ok():
            download.guard.download = None
            ip = download.ip
            id = download.connection.get_readable_id()
            self.kicked[ip] = id
            self.perip[ip].peerid = id
            self.kickfunc(download.connection)
        
    def try_ban(self, ip):
        if self._check_kicks_ok():
            self.banfunc(ip)
            self.banned[ip] = self.perip[ip].peerid
            if self.kicked.has_key(ip):
                del self.kicked[ip]

    def set_super_seed(self):
        self.super_seeding = True

    def check_complete(self, index):
        if self.endgamemode and not self.all_requests:
            self.endgamemode = False
        if self.endgame_queued_pieces and not self.endgamemode:
            self.requeue_piece_download()
        if self.picker.am_I_complete():
            assert not self.all_requests
            assert not self.endgamemode
            for d in [i for i in self.downloads if i.have.complete()]:
                d.connection.send_have(index)   # be nice, tell the other seed you completed
                self.add_disconnected_seed(d.connection.get_readable_id())
                d.connection.close()
            return True
        return False

    def too_many_partials(self):
        return len(self.storage.dirty) > (len(self.downloads)/2)


    def cancel_piece_download(self, pieces, allowrerequest=True):
        if self.endgamemode:
            if self.endgame_queued_pieces:
                for piece in pieces:
                    try:
                        self.endgame_queued_pieces.remove(piece)
                    except:
                        pass
            new_all_requests = []
            for index, nb, nl in self.all_requests:
                if index in pieces:
                    self.storage.request_lost(index, nb, nl)
                else:
                    new_all_requests.append((index, nb, nl))
            self.all_requests = new_all_requests

        for d in self.downloads:
            hit = False
            for index, nb, nl in d.active_requests:
                if index in pieces:
                    hit = True
                    d.connection.send_cancel(index, nb, nl)
                    if not self.endgamemode:
                        self.storage.request_lost(index, nb, nl)
            if hit:
                d.active_requests = [ r for r in d.active_requests
                                      if r[0] not in pieces ]
                # Arno: VOD: all these peers were slow for their individually 
                # assigned pieces. These pieces have high priority, so don't
                # retrieve any of theses pieces from these slow peers, just
                # give them something further in the future.
                if not allowrerequest:
                    # Arno: ALT is to just kick peer. Good option if we have lots (See Encryper.to_connect() queue
                    #print >>sys.stderr,"Downloader: Kicking slow peer",d.ip
                    #d.connection.close() # bye bye, zwaai zwaai
                    d._request_more(slowpieces=pieces)
                else:
                    d._request_more()
            if not self.endgamemode and d.choked:
                d._check_interests()

    def requeue_piece_download(self, pieces = []):
        if self.endgame_queued_pieces:
            for piece in pieces:
                if not piece in self.endgame_queued_pieces:
                    self.endgame_queued_pieces.append(piece)
            pieces = self.endgame_queued_pieces
        if self.endgamemode:
            if self.all_requests:
                self.endgame_queued_pieces = pieces
                return
            self.endgamemode = False
            self.endgame_queued_pieces = None
           
        ds = [d for d in self.downloads]
        shuffle(ds)
        for d in ds:
            if d.choked:
                d._check_interests()
            else:
                d._request_more()

    def start_endgame(self):
        assert not self.endgamemode
        self.endgamemode = True
        assert not self.all_requests
        for d in self.downloads:
            if d.active_requests:
                assert d.interested and not d.choked
            for request in d.active_requests:
                assert not request in self.all_requests
                self.all_requests.append(request)
        for d in self.downloads:
            d.fix_download_endgame()

    def pause(self, flag):
        self.paused = flag
        if flag:
            for d in self.downloads:
                for index, begin, length in d.active_requests:
                    d.connection.send_cancel(index, begin, length)
                d._letgo()
                d.send_not_interested()
            if self.endgamemode:
                self._reset_endgame()
        else:
            shuffle(self.downloads)
            for d in self.downloads:
                d._check_interests()
                if d.interested and not d.choked:
                    d._request_more()

    def live_invalidate(self,piece): # Arno: LIVEWRAP
        #print >>sys.stderr,"Downloader: live_invalidate",piece
        for d in self.downloads:
            d.have[piece] = False
        self.storage.live_invalidate(piece)
        
