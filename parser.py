# -*- coding: utf-8 -*-
import datetime
import json
import re
import threading
import time
import urllib2
from collections import Counter
from copy import copy
from logging import getLogger
from math import ceil
from random import choice, randint
from socket import timeout as SocketTimeout
from urllib import unquote
from urlparse import parse_qs

import pandora
from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ObjectDoesNotExist
from django.core.urlresolvers import reverse
from django.http import Http404
from django.utils.dateparse import parse_datetime
from haystack.query import SearchQuerySet
from scrapy.selector.lxmlsel import HtmlXPathSelector
from zope.interface.declarations import implements

import libs.n_timelog.n_timelog as nti
from apps.profiler.models import Profiler
from libs.utils.main_utils import slugify
from libs.utils.proxy import minimize_proxy_url
from libs.utils.thread_pool import threading_pool
from n.base.exceptions import ProductNotFound
from n.base.interfaces import INodeParser
from n.base.models import (
    NSeller, NProduct, NCategory, REASON_FOR_REFUSAL)
from n.base.result import ErrorResult
from n.base.sources.mixins import MakeNObjectFromDict, OfflineProductsParserMixin
from n.base.sources.taobao.models import OTApiEntryPointCH
from n.base.sources.taobao.otapi import OTApiSearchItemsParameters, OTApiProductList, OTApiNotAvailable, \
    OTApiProduct
from n.base.sources.tao.parser_h5api import h5ApiTao
from n.base.sources.tao.parser_product_api import TaoParserProductApi
from n.base.sources.tao.parser_request import get_tao_resploses
from n.base.sources.taobao.parser_search_selenium import TaoSeleniumSearchParser
from n.base.sources.taobao.product_photo_comments import ProductPhotoComments
from n.base.sources.taobao.russification import TaoProductMetaInfo
from n.base.sources.taobao.settings import TAO_BRAND_ID, Settings as TaoSettings
from n.base.tasks import send_email_for_consultant
from n.base.types import T_TAO
from n.sanitizers.table_sizes import save_tables
from n.statusflags import Flags
from n.translator.exceptions import DailyLimitExceeded
from n.translator.translator import SingletonTranslator
from n.utils.http import TimeoutContext
from n.utils.http.base import HtmlResponse
from n.utils.urls import request_dict_put_url_extra_data

class NodeParser(TaoParserProduct, MakeNObjectFromDict, TaoParserProductApi, ProductPhotoComments):

    implements(INodeParser)
    settings = {}

    re_num = re.compile(r'(\d+\.?(\d+)?)', re.U)
    re_find_word = re.compile(r'\b\w+?\b|\W+?', re.U)
    re_sizes = re.compile(r'\b(S|M|L|XL|XXL|XXXL|XXXXL|2XL|3XL|4XL)\b', re.U)
    
    def _search_product(self, kwargs, sort_by=None, brand_id=None, offset=0, limit=24, per_page=24):
        from time import sleep
        cat_id = kwargs['category']

        seller = kwargs.get('seller_inst')
        seller_id = seller.seller_id if seller else None
        if seller_id and isinstance(seller_id, (str, unicode)):
            if '_' in seller_id:
                seller_id = seller_id.split('_')[0]
        api_search_params = OTApiSearchItemsParameters()
        api_search_params.update({
            'ItemTitle': kwargs.get('query'),
            'LanguageOfQuery': 'zh-chs' if TaoSettings.get('SearchQueryTranslateToCN', True) else 'ru',
            'CategoryId': cat_id,
            'Provider': 'Tao',
            'OrderBy': sort_by,
            'VendorId': seller_id,
            'BrandId': brand_id,
            'MinPrice': kwargs.get('min_price'),
            'MaxPrice': kwargs.get('max_price'),
            'CurrencyCode': 'CNY',
            'SearchMethod': TaoSettings.get('DefaultSearchMethod', 'Official')
        })
        if kwargs['request']:
            invalidate_otapi_cache = int(kwargs['request'].GET.get('invalidate_otapi_cache', '0'))
        else:
            invalidate_otapi_cache = False

        try:
            request = kwargs['request']
            OTApiEntryPointCH(
                url=request.path if request else '',
                ip=request.META.get('HTTP_X_REAL_IP', request.META.get('REMOTE_ADDR', '')) if request else '',
                entry_class='OTApiProductList',
                entry_args=unicode(api_search_params),
                entry_kwargs=str(dict(use_cache=not invalidate_otapi_cache)),
                entry_file=__file__,
                entry_mark=7
            ).save()
            products = OTApiProductList(api_search_params, use_cache=not invalidate_otapi_cache)
            products_limited = products[offset:limit]
            extra_products = []
            extra_products_meta = None

            if len(products_limited) < per_page:
                need_extra_products = per_page - len(products_limited)
                res = self.search_product_local(kwargs, brand_id, sort_by, limit=need_extra_products)
                extra_products = res['results']
                extra_products_meta = res['meta']

            cats_ids = set()
            cats_api = OTApiProductList.extract_categories(products_limited)
            cats_local = [p['orig_category'] for p in extra_products if 'orig_category' in p]
            for p in cats_api:
                cats_ids.add(p.id)
            for p in cats_local:
                cats_ids.add(p.id)
            cats = NCategory.objects.filter(id__in=cats_ids)
            # API не отдает количество товаров по категориям, по-этому костыль здесь (cnt: 0)

            # в каталоге description у товара не требуется, поэтому указываются свои extra_attrs

            ___st = datetime.datetime.now()
            print '\n\n main products count \n\n'

            __countV = 0
            result_products = []

            required_attrs = [field.name for field in NProduct._meta.fields]
            extra_attrs = ('ncategory__item_id', 'parsed_seller_nick', 'image')
            required_attrs.extend(extra_attrs)
            print "\n\n\n attributes:\n"
            for attr in required_attrs:
                print attr
            for p in products_limited:
                __countV += 1
                print (__countV)
                result_products.append(p._as_dict(required_attrs))
                #result_products.append(p.as_dict(extra_attrs=('ncategory__item_id', 'parsed_seller_nick', 'image')))

            print '\n\n end of main products count for {}\n\n'.format(datetime.datetime.now() - ___st)
            #result_products = [
            #    p.as_dict(extra_attrs=('ncategory__item_id', 'parsed_seller_nick', 'image'))
            #    for p in products_limited
            #]

            source = 'OTAPI'
            if len(extra_products) > 0:
                source += ', elastic ({} + {})'.format(len(products), len(extra_products))
            for p in extra_products:
                result_products.append(p)
            print source
            result = {
                'used_proxy': None,
                'count': len(products) + len(extra_products),
                'categories': [{'item_id': c.item_id, 'cnt': 0} for c in cats],
                'results': result_products,
                'categories_by_additional_item_id': False,  # Иначе не все категории
                'source': source,
                'meta': extra_products_meta,
            }

        except OTApiNotAvailable:
            print ("\n\n !!!!!!!!!!!!!! Not Available !!!!!!!!!!!!!\n\n ")
            result = {
                'used_proxy': None,
                'count': 0,
                'categories': [],
                'results': [],
                'source': 'OTAPI not available',
            }
        except Http404:
            print ("\n\n !!!!!!!!!!!!!! 404 !!!!!!!!!!!!!\n\n ")
            result = {
                'used_proxy': None,
                'count': 0,
                'categories': [],
                'results': [],
                'source': 'OTAPI not found',
            }
        return result
