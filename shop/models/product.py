# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from distutils.version import LooseVersion
from functools import reduce
import operator
from cms import __version__ as CMS_VERSION
from django.core import checks
from django.db import models
from django.db.models.aggregates import Sum
from django.db.models.functions import Coalesce
from django.utils import six
from django.utils import timezone
from django.utils.encoding import force_text
from django.utils.six.moves.urllib.parse import urljoin
from django.utils.translation import ugettext_lazy as _
from polymorphic.managers import PolymorphicManager
from polymorphic.models import PolymorphicModel
from shop import deferred
from shop.conf import app_settings


class NotAvailable(Exception):
    pass


class Availability(object):
    """
    Contains the currently available quantity for a given product and period.
    """
    def __init__(self, **kwargs):
        """
        :param earliest: Point in time from when this product will be available.
        :param latest: Point in time until this product will be available.
        :param quantity: Number of available items.
        :param sell_short: Sell the product, even though it's not in stock. It
            will be shipped at the ``earliest`` point in time.
        :param limited_offer: Sell the product until the ``latest`` point in time.
        """
        self.earliest = kwargs.get('earliest', timezone.datetime.min)
        self.latest = kwargs.get('latest', timezone.datetime.max)
        quantity = kwargs.get('quantity', app_settings.MAX_PURCHASE_QUANTITY)
        self.quantity = min(quantity, app_settings.MAX_PURCHASE_QUANTITY)
        self.sell_short = bool(kwargs.get('sell_short', False))
        self.limited_offer = bool(kwargs.get('limited_offer', False))
        self.inventory = bool(kwargs.get('inventory', None))


class AvailableProductMixin(object):
    """
    Add this mixin class to the product models declaration, wanting to keep track on the
    current amount of products in stock. In comparison to
    :class:`shop.models.product.ReserveProductMixin`, this mixin does not reserve items in pending
    carts, with the risk for overselling. It thus is suited for products kept in the cart
    for a long period.

    The product class must implement a field named ``quantity`` accepting numerical values.
    """
    def get_availability(self, request, **extra):
        """
        Returns the current available quantity for this product.

        If other customers have pending carts containing this same product, the quantity
        is not not adjusted. This may result in a situation, where someone adds a product
        to the cart, but then is unable to purchase, because someone else bought it in the
        meantime.
        """
        return Availability(quantity=self.quantity)

    def deduct_from_stock(self, quantity, **extra):
        self.quantity -= quantity
        self.save(update_fields=['quantity'])

    @classmethod
    def check(cls, **kwargs):
        errors = super(AvailableProductMixin, cls).check(**kwargs)
        allowed_types = ['IntegerField', 'SmallIntegerField', 'PositiveIntegerField',
                         'PositiveSmallIntegerField', 'DecimalField', 'FloatField']
        for field in cls._meta.fields:
            if field.attname == 'quantity':
                if field.get_internal_type() not in allowed_types:
                    msg = "Class `{}.quantity` must be of one of the types: {}."
                    errors.append(checks.Error(msg.format(cls.__name__, allowed_types)))
                break
        else:
            msg = "Class `{}` must implement a field named `quantity`."
            errors.append(checks.Error(msg.format(cls.__name__)))
        return errors


class _ReserveProductMixin(object):
    def get_availability(self, request, **extra):
        """
        Returns the current available quantity for this product.

        If other customers have pending carts containing this same product, the quantity
        is adjusted accordingly. Therefore make sure to invalidate carts, which were not
        converted into an order after a determined period of time. Otherwise the quantity
        returned by this function might be considerably lower, than what it could be.
        """
        from shop.models.cart import CartItemModel

        availability = super(ReserveProductMixin, self).get_availability(request)
        cart_items = CartItemModel.objects.filter(product=self).values('quantity')
        availability.quantity -= cart_items.aggregate(sum=Coalesce(Sum('quantity'), 0))['sum']
        return availability


class ReserveProductMixin(_ReserveProductMixin, AvailableProductMixin):
    """
    Add this mixin class to the product models declaration, wanting to keep track on the
    current amount of products in stock. In comparison to
    :class:`shop.models.product.AvailableProductMixin`, this mixin reserves items in pending
    carts, without the risk for overselling. On the other hand, the shop may run out of sellable
    items, if customers keep products in the cart for a long period, without proceeding to checkout.
    Use this mixin for products kept for a short period until checking out the cart, for
    instance for ticket sales. Ensure that pending carts are flushed regularly.

    The product class must implement a field named ``quantity`` accepting numerical values.
    """


class BaseProductManager(PolymorphicManager):
    """
    A base ModelManager for all non-object manipulation needs, mostly statistics and querying.
    """
    def select_lookup(self, search_term):
        """
        Returning a queryset containing the products matching the declared lookup fields together
        with the given search term. Each product can define its own lookup fields using the
        member list or tuple `lookup_fields`.
        """
        filter_by_term = (models.Q((sf, search_term)) for sf in self.model.lookup_fields)
        queryset = self.get_queryset().filter(reduce(operator.or_, filter_by_term))
        return queryset

    def indexable(self):
        """
        Return a queryset of indexable Products.
        """
        queryset = self.get_queryset().filter(active=True)
        return queryset


class PolymorphicProductMetaclass(deferred.PolymorphicForeignKeyBuilder):

    @classmethod
    def perform_meta_model_check(cls, Model):
        """
        Perform some safety checks on the ProductModel being created.
        """
        if not isinstance(Model.objects, BaseProductManager):
            msg = "Class `{}.objects` must provide ModelManager inheriting from BaseProductManager"
            raise NotImplementedError(msg.format(Model.__name__))

        if not isinstance(getattr(Model, 'lookup_fields', None), (list, tuple)):
            msg = "Class `{}` must provide a tuple of `lookup_fields` so that we can easily lookup for Products"
            raise NotImplementedError(msg.format(Model.__name__))

        if not callable(getattr(Model, 'get_price', None)):
            msg = "Class `{}` must provide a method implementing `get_price(request)`"
            raise NotImplementedError(msg.format(cls.__name__))


class BaseProduct(six.with_metaclass(PolymorphicProductMetaclass, PolymorphicModel)):
    """
    An abstract basic product model for the shop. It is intended to be overridden by one or
    more polymorphic models, adding all the fields and relations, required to describe this
    type of product.

    Some attributes for this class are mandatory. They shall be implemented as property method.
    The following fields MUST be implemented by the inheriting class:
    ``product_name``: Return the pronounced name for this product in its localized language.

    Additionally the inheriting class MUST implement the following methods ``get_absolute_url()``
    and ``get_price()``. See below for details.

    Unless each product variant offers it's own product code, it is strongly recommended to add
    a field ``product_code = models.CharField(_("Product code"), max_length=255, unique=True)``
    to the class implementing the product.
    """
    created_at = models.DateTimeField(
        _("Created at"),
        auto_now_add=True,
    )

    updated_at = models.DateTimeField(
        _("Updated at"),
        auto_now=True,
    )

    active = models.BooleanField(
        _("Active"),
        default=True,
        help_text=_("Is this product publicly visible."),
    )

    class Meta:
        abstract = True
        verbose_name = _("Product")
        verbose_name_plural = _("Products")

    def product_type(self):
        """
        Returns the polymorphic type of the product.
        """
        return force_text(self.polymorphic_ctype)
    product_type.short_description = _("Product type")

    @property
    def product_model(self):
        """
        Returns the polymorphic model name of the product's class.
        """
        return self.polymorphic_ctype.model

    def get_absolute_url(self):
        """
        Hook for returning the canonical Django URL of this product.
        """
        msg = "Method get_absolute_url() must be implemented by subclass: `{}`"
        raise NotImplementedError(msg.format(self.__class__.__name__))

    def get_price(self, request):
        """
        Hook for returning the current price of this product.
        The price shall be of type Money. Read the appropriate section on how to create a Money
        type for the chosen currency.
        Use the `request` object to vary the price according to the logged in user,
        its country code or the language.
        """
        msg = "Method get_price() must be implemented by subclass: `{}`"
        raise NotImplementedError(msg.format(self.__class__.__name__))

    def get_product_variant(self, **kwargs):
        """
        Hook for returning the variant of a product using parameters passed in by **kwargs.
        If the product has no variants, then return the product itself.

        :param **kwargs: A dictionary describing the product's variations.
        """
        return self

    def get_availability(self, request, **kwargs):
        """
        Hook for checking the availability of a product.

        :param request:
            Optionally used to vary the availability according to the logged in user,
            its country code or language.

        :param **kwargs:
            Extra arguments passed to the underlying method. Useful for products with
            variations.

        :return: An object of type :class:`shop.models.product.Availability`.
        """
        return Availability()

    def is_in_cart(self, cart, watched=False, **kwargs):
        """
        Checks if the current product is already in the given cart, and if so, returns the
        corresponding cart_item.

        :param watched (bool): This is used to determine if this check shall only be performed
            for the watch-list.

        :param **kwargs: Optionally one may pass arbitrary information about the product being looked
            up. This can be used to determine if a product with variations shall be considered
            equal to the same cart item, resulting in an increase of it's quantity, or if it
            shall be considered as a separate cart item, resulting in the creation of a new item.

        :returns: The cart item (of type CartItem) containing the product considered as equal to the
            current one, or ``None`` if no product matches in the cart.
        """
        from shop.models.cart import CartItemModel
        cart_item_qs = CartItemModel.objects.filter(cart=cart, product=self)
        return cart_item_qs.first()

    def deduct_from_stock(self, quantity, **kwargs):
        """
        Hook to deduct a number of items of the current product from the stock's inventory.

        :param quantity: Number of items to deduct.

        :param **kwargs:
            Extra arguments passed to the underlying method. Useful for products with
            variations.
        """

    def get_weight(self):
        """
        Optional hook to return the product's gross weight in kg. This information is required to
        estimate the shipping costs. The merchants product model shall override this method.
        """
        return 0

    @classmethod
    def check(cls, **kwargs):
        errors = super(BaseProduct, cls).check(**kwargs)
        try:
            cls.product_name
        except AttributeError:
            msg = "Class `{}` must provide a model field implementing `product_name`"
            errors.append(checks.Error(msg.format(cls.__name__)))
        return errors

ProductModel = deferred.MaterializedModel(BaseProduct)


class CMSPageReferenceMixin(object):
    """
    Products which refer to CMS pages in order to emulate categories, normally need a method for
    being accessed directly through a canonical URL. Add this mixin class for adding a
    ``get_absolute_url()`` method to any to product model.
    """
    category_fields = ['cms_pages']  # used by ProductIndex to fill the categories

    def get_absolute_url(self):
        """
        Return the absolute URL of a product
        """
        # sorting by highest level, so that the canonical URL
        # associates with the most generic category
        if LooseVersion(CMS_VERSION) < LooseVersion('3.5'):
            cms_page = self.cms_pages.order_by('depth').last()
        else:
            cms_page = self.cms_pages.order_by('node__path').last()
        if cms_page is None:
            return urljoin('/category-not-assigned/', self.slug)
        return urljoin(cms_page.get_absolute_url(), self.slug)
