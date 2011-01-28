# -*- coding: utf-8 -*-
import datetime
import os
import shutil
import socket
import time

from django.contrib.sites.models import Site
from django.conf import settings
from django.test.client import Client as TestClient

from mock import patch, Mock
from nose import SkipTest
from nose.tools import eq_
from pyquery import PyQuery as pq
import test_utils

from input.urlresolvers import reverse
import feedback
from input import (OPINION_PRAISE, OPINION_ISSUE, OPINION_SUGGESTION,
                   OPINION_RATING, OPINION_BROKEN, OPINION_TYPES_USAGE)

from feedback import FIREFOX
from feedback.cron import populate
from feedback.models import Opinion
from search import views
from search.client import Client, SearchError, extract_filters
from search.utils import start_sphinx, stop_sphinx, reindex


# TODO(davedash): liberate from Zamboni
class SphinxTestCase(test_utils.TransactionTestCase):
    """
    This test case type can setUp and tearDown the sphinx daemon.  Use this
    when testing any feature that requires sphinx.
    """

    fixtures = ('feedback/opinions',)
    sphinx = True
    sphinx_is_running = False

    def setUp(self):
        super(SphinxTestCase, self).setUp()

        settings.SITE_ID = settings.DESKTOP_SITE_ID

        if not SphinxTestCase.sphinx_is_running:
            if (not settings.SPHINX_SEARCHD or
                not settings.SPHINX_INDEXER):  # pragma: no cover
                raise SkipTest()

            os.environ['DJANGO_ENVIRONMENT'] = 'test'

            if os.path.exists(settings.TEST_SPHINX_CATALOG_PATH):
                shutil.rmtree(settings.TEST_SPHINX_CATALOG_PATH)
            if os.path.exists(settings.TEST_SPHINX_LOG_PATH):
                shutil.rmtree(settings.TEST_SPHINX_LOG_PATH)

            os.makedirs(settings.TEST_SPHINX_LOG_PATH)
            os.makedirs(settings.TEST_SPHINX_CATALOG_PATH)

            reindex()
            start_sphinx()
            time.sleep(1)
            SphinxTestCase.sphinx_is_running = True

    @classmethod
    def tearDownClass(cls):
        if SphinxTestCase.sphinx_is_running:
            stop_sphinx()
            SphinxTestCase.sphinx_is_running = False

query = lambda x='', **kwargs: Client().query(x, **kwargs)
num_results = lambda x='', **kwargs: len(query(x, **kwargs))


class SearchTest(SphinxTestCase):

    def test_query(self):
        eq_(num_results(), 31)

    def test_url_search(self):
        eq_(num_results('url:*'), 7)

    def test_result_set(self):
        rs = query()
        assert isinstance(rs[0], Opinion)

    def test_default_ordering(self):
        """Any query should return results in rev-chron order."""
        r = query()
        dates = [o.created for o in r]
        eq_(dates, sorted(dates, reverse=True), "These aren't revchron.")

        r = query('Firefox')
        dates = [o.created for o in r]
        eq_(dates, sorted(dates, reverse=True), "These aren't revchron.")

    def test_product_filter(self):
        eq_(num_results(product=1), 31)
        eq_(num_results(product=2), 0)

    def test_version_filter(self):
        eq_(num_results(version='3.6.3'), 11)
        eq_(num_results(version='3.6.4'), 16)

    def test_type_filter(self):
        eq_(num_results(type=OPINION_PRAISE.id), 17)
        eq_(num_results(type=OPINION_ISSUE.id), 11)
        eq_(num_results(type=OPINION_SUGGESTION.id), 3)

    def test_os_filter(self):
        eq_(num_results(os='mac'), 31)
        eq_(num_results(os='palm'), 0)

    def test_locale_filter(self):
        eq_(num_results(locale='en-US'), 29)
        eq_(num_results(locale='de'), 1)
        eq_(num_results(locale='unknown'), 1)

    def test_date_filter(self):
        start = datetime.datetime(2010, 5, 27)
        end = datetime.datetime(2010, 5, 27)
        eq_(num_results(date_start=start, date_end=end), 5)

    @patch('search.client.sphinx.SphinxClient.RunQueries')
    def test_errors(self, sphinx):
        for error in (socket.timeout(), Exception(),):
            sphinx.side_effect = error
            self.assertRaises(SearchError, query)

    @patch('search.client.sphinx.SphinxClient.GetLastError')
    def test_getlasterror(self, sphinx):
        sphinx = lambda: True
        self.assertRaises(SearchError, query)


def search_request(product='firefox', **kwargs):
    kwargs['product'] = product
    kwargs['version'] = '--'
    return TestClient().get(reverse('search', channel='beta'),
                            kwargs, follow=True)


class NoRatingsSearchTest(SphinxTestCase):
    """Ratings in search results is an abomination. -wenzel"""
    fixtures = []

    def setUp(self):
        populate(20, 'desktop', OPINION_RATING)
        populate(2, 'desktop', OPINION_SUGGESTION)
        super(NoRatingsSearchTest, self).setUp()

    def test_search_page(self):
        r = search_request()
        doc = pq(r.content)
        eq_(len(doc('.message')), 2)


class SearchViewTest(SphinxTestCase):
    """Tests relating to the search template rendering."""

    def setUp(self):
        # add more opinions so we can test things.
        populate(1000, 'desktop', OPINION_SUGGESTION)
        populate(100, 'mobile', OPINION_SUGGESTION)
        super(SearchViewTest, self).setUp()

    def test_pagination_max(self):
        r = search_request(page=700)
        self.failUnlessEqual(r.status_code, 200)

    def test_next_page(self):
        r = search_request()
        assert pq(r.content)('.pager a.next')

    def test_no_next_page(self):
        """Once we're on page 50, let's not show an older messages link."""
        for page in (50, 51, 100, 200):
            r = search_request(page=page)
            doc = pq(r.content)
            assert not doc('.pager a.next')

    def test_filters(self):
        """
        Make sure Manufacturer and Device filters don't show up on search for
        desktop.
        """
        r = search_request()
        doc = pq(r.content)
        filters = doc('.filter h3 a')
        assert not any(['Manufacturer' in f.text_content() for f in filters])
        assert not any(['Device' in f.text_content() for f in filters])
        r = search_request(product='mobile')
        doc = pq(r.content)
        filters = doc('.filter h3 a')
        assert any(['Manufacturer' in f.text_content() for f in filters])
        assert any(['Device' in f.text_content() for f in filters])

    def compare_2_pages(self, page1, page2):
        r = search_request(page=page1)
        doc = pq(r.content)
        firstmsg = doc('.message').eq(1).text()
        r = search_request(page=page2)
        doc = pq(r.content)
        self.assertNotEqual(firstmsg, doc('.message').eq(1).text())

    def test_page_2(self):
        self.compare_2_pages(1, 2)

    def test_pages_4_and_5(self):
        """
        There was a bug where we kept showing the same page, after page 4.
        """
        self.compare_2_pages(4, 5)

    def test_page_0(self):
        """In bug 620296, page 0 led to an AssertionError."""
        for page in (-1, 0):
            r = search_request(page=page)
            eq_(r.status_code, 200)
            eq_(r.context['form'].cleaned_data['page'], 1)

    @patch('search.views._get_results')
    def test_error(self, get_results):
        get_results.side_effect = SearchError()
        r = search_request()
        eq_(r.status_code, 500)

    def test_atom_link(self):
        r = search_request()
        doc = pq(r.content)
        eq_(len(doc('link[type="application/atom+xml"]')), 1)

    def test_flipped_date_filter(self):
        """No error if start > end."""
        r = search_request(date_start='2010-09-01', date_end='2010-06-01')
        eq_(r.status_code, 200)

    def test_carets(self):
        """Rotten queries should not phase us."""
        r = search_request(q='^')
        eq_(r.status_code, 200)

    def test_drop_page_on_new_search(self):
        """
        Make sure `page` paramter is dropped when changing search
        parameters. Bug 617211.
        """
        r = search_request(page=2)
        eq_(r.status_code, 200)
        doc = pq(r.content)
        eq_(doc('#kw-search input[name="page"]').length, 0)
        eq_(doc('#filters input[name="page"]').length, 0)

    def test_search_without_date(self):
        """Ensure searching without date does not restrict by date."""
        data = {
            'product': 'firefox',
        }
        r = self.client.get(reverse('search'), data, follow=True)
        eq_(r.status_code, 200)
        assert not r.context['form'].cleaned_data.get('date_start')
        assert not r.context['form'].cleaned_data.get('date_end')

    def test_search_with_invalid_date(self):
        """Date validation should not error out."""
        data = {
            'product': 'firefox',
            'date_start': 'cheesecake',
            'date_end': '',
        }
        r = self.client.get(reverse('search'), data, follow=True)
        eq_(r.status_code, 200)
        assert not hasattr(r.context['form'], 'cleaned_data')

    def test_when_selectors(self):
        """Test that the date selectors show the right dates."""
        r = search_request()
        doc = pq(r.content)
        date_selectors = doc('#when li a')

        # We expect 1, 7, 30 days, infinity in that order.
        ago = lambda x: datetime.date.today() - datetime.timedelta(days=x)
        expect = (ago(1), ago(7), ago(30), None, None)
        for n, a in enumerate(date_selectors):
            a = pq(a)
            link = a.attr('href')
            if expect[n]:
                assert link.find('date_start=%s' %
                                 expect[n].strftime('%Y-%m-%d')) >= 0
            else:
                assert link.find('date_start') == -1
            assert link.find('date_end') == -1  # Never add end date.

    def test_bogus_parameters(self):
        """
        Bogus GET parameters must neither break the page nor persist through
        subsequent searches.
        """
        r = search_request(hello='world')
        doc = pq(r.content)

        eq_(r.status_code, 200)
        eq_(doc('input[name="hello"]').length, 0)


class FeedTest(SphinxTestCase):
    def _pq(self, response):
        """PyQuery-fy a response."""
        return pq(response.content.replace('xmlns', 'xmlfail'))

    def test_invalid_form(self):
        # Sunbird is always the wrong product.
        r = self.client.get(reverse('search.feed'), {'search': 'sunbird'},
                            True)
        self.failUnlessEqual(r.status_code, 200)

    def test_title(self):
        r = self.client.get(reverse('search.feed'),
                            {'product': 'firefox', 'q': 'lol'})
        doc = self._pq(r)
        eq_(doc('title').text(), "Firefox Input: 'lol'")

    def test_unicode_title(self):
        """Unicode in search queries must not fail. Bug 606001."""
        r = self.client.get(reverse('search.feed'),
                            {'product': 'firefox', 'q': u'é'})
        doc = self._pq(r)
        eq_(doc('title').text(), u"Firefox Input: 'é'")

    def test_query(self):
        r = self.client.get(reverse('search.feed'),
                            dict(product='firefox',
                                 version='--',
                                 date_start='01/01/2000',
                                 date_end='01/01/2031'))
        doc = self._pq(r)
        s = Site.objects.all()[0]
        url_base = 'http://%s/%s/%s/' % (s.domain, 'en-US',
                                        settings.DEFAULT_CHANNEL)
        eq_(doc('entry link').attr['href'],
            '%s%s' % (url_base, 'opinion/29'))

    def test_item_title(self):
        """
        If we don't convert opinion type names to unicode, the world will end.
        Bug 617204.
        """
        r = self.client.get(reverse('search.feed', channel='beta'),
                            dict(product='firefox', version='--'))
        doc = self._pq(r)
        # If we get a memory address, this is not a unicode string.
        eq_(doc('entry title').text().find('object at 0x'), -1)

    @patch('search.views.SearchFeed.get_object')
    @patch('search.views.SearchFeed.items')
    def test_opinion_types(self, *args):
        def mock_get_object(self, request):
            return {'request': request}
        views.SearchFeed.get_object = mock_get_object

        def mock_items(self, obj):
            return [Opinion(id=n, type=type.id, product=FIREFOX.id) for
                    n, type in enumerate(OPINION_TYPES_USAGE)]
        views.SearchFeed.items = mock_items

        r = self.client.get(reverse('search.feed'))
        eq_(r.status_code, 200)


def test_get_sentiment():
    r = views.get_sentiment([{'type': OPINION_ISSUE.id, 'count': 1}])
    eq_(r['sentiment'], 'sad')


@patch('search.forms.ReporterSearchForm.is_valid')
def test_get_results(is_valid):
    is_valid.return_value = False
    request = Mock()
    request.GET = {}
    request.default_app = FIREFOX
    r = views._get_results(request)
    eq_(r[2], request.default_app)


def test_date_filter_timezone():
    """Ensure date filters are applied in app time (= PST), not UTC."""
    dates = dict(date_start=datetime.date(2010, 1, 1),
                   date_end=datetime.date(2010, 1, 31))
    _, ranges, _ = extract_filters(dates)
    eq_(ranges['created'][0], 1262332800)  # 8:00 UTC on 1/1/2010
    # 8:00 UTC on 2/1/2010 (sic, to include all of the last day)
    eq_(ranges['created'][1], 1265011200)
