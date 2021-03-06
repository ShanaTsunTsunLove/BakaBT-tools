import re
import shutil
import urllib
import mechanize
import os.path
import HTMLParser
import sys

class BakaParser(HTMLParser.HTMLParser):
    waiting_for_pages = False
    page_links = []

    def handle_starttag(self, tag, attrs):
        if tag == 'div':
            for a in attrs:
                if a[0] == 'class' and a[1] == 'pager':
                    BakaParser.waiting_for_pages = True

        if BakaParser.waiting_for_pages and tag == 'a':
            for a in attrs:
                if a[0] == 'href':
                    BakaParser.page_links.append(a[1])

    def handle_endtag(self, tag):
        if tag == 'div' and BakaParser.waiting_for_pages:
            BakaParser.waiting_for_pages = False

class Either(object):

    def __init__(self, val, left=False):
        self.val = val
        self.left = left

    def __repr__(self):
        if self.is_left():
            return 'Left (%s)' % self.val
        return 'Right (%s)' % self.val

    def is_left(self):
        return self.left

    def get_value(self):
        return self.val

    def bind(self, f):
        if self.is_left():
            return self
        return f(self.get_value())

    def __rshift__(self, f):
        return self.bind(lambda _: f)

    @staticmethod
    def mreturn(v):
        return Right(v)


class Maybe(object):

    def __init__(self, val, nothing=False):
        self.val = val
        self.nothing = nothing

    def __repr__(self):
        if self.is_nothing():
            return 'Nothing'
        return 'Just (%s)' % self.val

    def is_nothing(self):
        return self.nothing

    def get_value(self):
        if self.nothing:
            raise Maybe.NothingError
        return self.val

    def __rshift__(self, f):
        return self.bind(lambda _: f)

    def bind(self, f):
        if self.is_nothing():
            return Nothing
        return f(self.get_value())

    @staticmethod
    def mreturn(v):
        return Just(v)

    class NothingError(Exception):
        pass


Nothing = Maybe(None, nothing=True)
Just = Maybe
Left = lambda x: Either(x, left=True)
Right = Either

def comp(f, g):
    return lambda x: f(g(x))

def concat(xs):
    return [ y for x in xs for y in x ]

def klesli_comp(f, g):
    return lambda x: g(x).bind(f)

def liftM(f, m):
    return m.bind(lambda x: type(m).mreturn(f(x)))

def sequenceE(ms):
    p = Right([])
    for m in ms:
        if m.is_left():
            return m
        p = p.bind(lambda x: type(p).mreturn(x + [m.get_value()]))
    return p

def mapE(f, ms):
    return sequenceE( [ f(m) for m in ms ])

def fmapM(f, m):
    return m.bind(lambda x: map(f, x))

# (a -> b) -> Maybe [a] -> Maybe [b]
def mapMaybe(f, m):
    if m.is_nothing():
        return Nothing
    return Just([ f(x) for x in m.get_value() ])

def get_links(conf):
    def inner_links(page_source):
        sections = []
        while '<td class="category"' in page_source:
            cutOff = page_source.find('<td class="category"')
            sections.append(page_source[:cutOff])
            page_source = page_source[cutOff + 24:]

        sections.append(page_source)

        splitAlts = []
        for s in sections:
            if 'Alternative versions:' in s:
                x = s.find('Alternative versions:')
                splitAlts.append(s[:x])
                splitAlts.append(s[x:])
            else:
                splitAlts.append(s)
                fr = 'title="Freeleech">[F]</span>'
                sections = [s for s in sections
                            if fr in s or not conf.no_freeleech]

        extracted = []
        for s in sections:
            size = re.search(r'<td class="size">([0-9]*\.?[0-9]+)'
                             + r'\s(\w+)</td>',
                             s).groups()
            mb_size = convert_to_mb(size[0], size[1])

            if mb_size > conf.maxsize:
                continue

            extracted.append(re.search(r'<a href="(/\d+[-_' +
                                       r'\w.]+)" style="color:', s).groups()[0])

        return [ conf.website + x for x in extracted ]

    return inner_links

def convert_to_mb(number, unit):
    units = {'B': 2, 'KB': 1, 'MB': 0,
             'GB': -1, 'TB': -2, 'PB': -2}
    return float(number) * (1024 ** units[unit])


def get_torrent_url(conf):
    def inner(url):
        source = get_page_source(conf)(url)
        f = lambda x: conf.website + re.search(
            r'<a href="(/download/\d+/\d+/'
            + r'\w+/\d+/[\w_.-]+.torrent)"', x).groups()[0]

        return liftM(f, source)

    return inner

def download(conf):
    def inner_download(url):
        try:
            filename = os.path.join(conf.directory,
                                    shutil.os.path.split(url)[1])

            if not os.path.exists(conf.directory):
                os.makedirs(conf.directory)

            urllib.urlretrieve(url, filename)

        except urllib.ContentTooShortError:
            return Left('ContentTooShortError when downloading %s to %s'
                        % (url, filename))
        except IOError:
            return Left('IOError when downloading %s to %s' % (url, filename))
        except Exception as e:
            return Left('%s' % e)

        if conf.verbose: sys.stdout.write('Dowloaded: %s\n' % filename)
        return Right('%s downloaded to %s' % (url, filename))

    return inner_download

def login(conf):
    try:
        username = conf.username
        password = conf.password
        request = mechanize.Request('%s/login.php' % conf.website)
        response = mechanize.urlopen(request, timeout=conf.timeout)
        forms = mechanize.ParseResponse(response)
        response.close()

        if len(forms) < 3:
            return Left('Failed to reach the login page.')

        form = forms[2]
        form['username'] = username
        form['password'] = password
        login_request = form.click()

        login_response = mechanize.urlopen(login_request, timeout=conf.timeout)
        logged_in = login_response.geturl() == ('%s/index.php'
                                                % conf.website)

        if not logged_in:
            return Left('Failed to log in with these credentials')

    except mechanize.HTTPError as resp:
        return Left('HTTPError when logging in: %s' % resp)
    except Exception as e:
        return Left('%s' % e)

    if conf.verbose: sys.stdout.write('Logged in as %s\n' % username)
    return Right('Logged in as %s' % username)

def get_page_source(conf):
    def inner(url):
        try:
            return Right(mechanize.urlopen(url, timeout=conf.timeout).read())
        except mechanize.HTTPError:
            return Left('HTTPError when fetching %s' % url)
        except ValueError as ve:
            return Left('URL value error: %s' % ve)
        except Exception as e:
            return Left('%s' % e)
    return inner

def get_pages(conf):
    try:
        order = '&ordertype=size&order=1' if conf.smallest else ''
        bonus = '&only=1&bonus=1'

        amount = conf.amount
        if amount < 1:
            amount = 1
        elif amount > 100:
            amount = 100

        page_url = ('%s/browse.php?limit=%s%s%s' % (conf.website,
                                                    amount, order, bonus))

        request = mechanize.Request(page_url)
        response = mechanize.urlopen(request, timeout=conf.timeout)
        body = response.read()
        response.close()

        parser = BakaParser()
        parser.feed(body)
        pages = int(BakaParser.page_links[-2].split('=')[-1]) + 1
        pages = conf.limit if conf.limit > 0 else pages
        if conf.verbose: sys.stdout.write('Got %d pages\n' % pages)
        return Right([ '%s&page=%d' % (page_url, p) for p in xrange(pages) ])
    except IndexError:
        return Left('Failed to fetch number of pages')
    except mechanize.HTTPError as me:
        return Left('Failed to reach basic search results: %s' % me)
    except Exception as e:
        return Left('%s' % e)
