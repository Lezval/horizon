# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2011 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
# All Rights Reserved.
#
# Copyright 2011 Nebula, Inc.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
Methods and interface objects used to interact with external apis.

API method calls return objects that are in many cases objects with
attributes that are direct maps to the data returned from the API http call.
Unfortunately, these objects are also often constructed dynamically, making
it difficult to know what data is available from the API object.  Because of
this, all API calls should wrap their returned object in one defined here,
using only explicitly defined atributes and/or methods.

In other words, django_openstack developers not working on django_openstack.api
shouldn't need to understand the finer details of APIs for Nova/Glance/Swift et
al.
"""

import httplib
import json
import logging
import urlparse

from django.conf import settings
from django.contrib import messages

import cloudfiles
import openstack.compute
import openstackx.admin
import openstackx.api.exceptions as api_exceptions
import openstackx.extras
import openstackx.auth
from glance import client as glance_client
from glance.common import exception as glance_exceptions
from novaclient import client as base_nova_client
from novaclient import exceptions as nova_exceptions
from novaclient.v1_1 import client as nova_client
from quantum import client as quantum_client

LOG = logging.getLogger('django_openstack.api')


class APIResourceWrapper(object):
    """ Simple wrapper for api objects

        Define _attrs on the child class and pass in the
        api object as the only argument to the constructor
    """
    _attrs = []

    def __init__(self, apiresource):
        self._apiresource = apiresource

    def __getattr__(self, attr):
        if attr in self._attrs:
            # __getattr__ won't find properties
            return self._apiresource.__getattribute__(attr)
        else:
            LOG.debug('Attempted to access unknown attribute "%s" on'
                      ' APIResource object of type "%s" wrapping resource of'
                      ' type "%s"' % (attr, self.__class__,
                                      self._apiresource.__class__))
            raise AttributeError(attr)


class APIDictWrapper(object):
    """ Simple wrapper for api dictionaries

        Some api calls return dictionaries.  This class provides identical
        behavior as APIResourceWrapper, except that it will also behave as a
        dictionary, in addition to attribute accesses.

        Attribute access is the preferred method of access, to be
        consistent with api resource objects from openstackx
    """
    def __init__(self, apidict):
        self._apidict = apidict

    def __getattr__(self, attr):
        if attr in self._attrs:
            try:
                return self._apidict[attr]
            except KeyError, e:
                raise AttributeError(e)

        else:
            LOG.debug('Attempted to access unknown item "%s" on'
                      'APIResource object of type "%s"'
                      % (attr, self.__class__))
            raise AttributeError(attr)

    def __getitem__(self, item):
        try:
            return self.__getattr__(item)
        except AttributeError, e:
            # caller is expecting a KeyError
            raise KeyError(e)

    def get(self, item, default=None):
        try:
            return self.__getattr__(item)
        except AttributeError:
            return default


class Container(APIResourceWrapper):
    """Simple wrapper around cloudfiles.container.Container"""
    _attrs = ['name']


class Console(APIResourceWrapper):
    """Simple wrapper around openstackx.extras.consoles.Console"""
    _attrs = ['id', 'output', 'type']


class Flavor(APIResourceWrapper):
    """Simple wrapper around openstackx.admin.flavors.Flavor"""
    _attrs = ['disk', 'id', 'links', 'name', 'ram', 'vcpus']


class FloatingIp(APIResourceWrapper):
    """Simple wrapper for floating ips"""
    _attrs = ['ip', 'fixed_ip', 'instance_id', 'id']


class Image(APIDictWrapper):
    """Simple wrapper around glance image dictionary"""
    _attrs = ['checksum', 'container_format', 'created_at', 'deleted',
             'deleted_at', 'disk_format', 'id', 'is_public', 'location',
             'name', 'properties', 'size', 'status', 'updated_at', 'owner']

    def __getattr__(self, attrname):
        if attrname == "properties":
            return ImageProperties(super(Image, self).__getattr__(attrname))
        else:
            return super(Image, self).__getattr__(attrname)


class ImageProperties(APIDictWrapper):
    """Simple wrapper around glance image properties dictionary"""
    _attrs = ['architecture', 'image_location', 'image_state', 'kernel_id',
             'project_id', 'ramdisk_id']


class KeyPair(APIResourceWrapper):
    """Simple wrapper around openstackx.extras.keypairs.Keypair"""
    _attrs = ['fingerprint', 'name', 'private_key']


class Server(APIResourceWrapper):
    """Simple wrapper around openstackx.extras.server.Server

       Preserves the request info so image name can later be retrieved
    """
    _attrs = ['addresses', 'attrs', 'hostId', 'id', 'image', 'links',
             'metadata', 'name', 'private_ip', 'public_ip', 'status', 'uuid',
             'image_name', 'VirtualInterfaces']

    def __init__(self, apiresource, request):
        super(Server, self).__init__(apiresource)
        self.request = request

    def __getattr__(self, attr):
        if attr == "attrs":
            return ServerAttributes(super(Server, self).__getattr__(attr))
        else:
            return super(Server, self).__getattr__(attr)

    @property
    def image_name(self):
        try:
            image = image_get(self.request, self.image['id'])
            return image.name
        except glance_exceptions.NotFound:
            return "(not found)"

    def reboot(self, hardness=openstack.compute.servers.REBOOT_HARD):
        compute_api(self.request).servers.reboot(self.id, hardness)


class ServerAttributes(APIDictWrapper):
    """Simple wrapper around openstackx.extras.server.Server attributes

       Preserves the request info so image name can later be retrieved
    """
    _attrs = ['description', 'disk_gb', 'host', 'image_ref', 'kernel_id',
              'key_name', 'launched_at', 'mac_address', 'memory_mb', 'name',
              'os_type', 'tenant_id', 'ramdisk_id', 'scheduled_at',
              'terminated_at', 'user_data', 'user_id', 'vcpus', 'hostname',
              'security_groups']


class Services(APIResourceWrapper):
    _attrs = ['disabled', 'host', 'id', 'last_update', 'stats', 'type', 'up',
             'zone']


class SwiftObject(APIResourceWrapper):
    _attrs = ['name']


class Tenant(APIResourceWrapper):
    """Simple wrapper around openstackx.auth.tokens.Tenant"""
    _attrs = ['id', 'description', 'enabled', 'name']


class Token(object):
    def __init__(self, id=None, serviceCatalog=None, tenant_id=None, user=None):
        self.id = id
        self.serviceCatalog = serviceCatalog or {}
        self.tenant_id = tenant_id
        self.user = user or {}


class Usage(APIResourceWrapper):
    """Simple wrapper around openstackx.extras.usage.Usage"""
    _attrs = ['begin', 'instances', 'stop', 'tenant_id',
             'total_active_disk_size', 'total_active_instances',
             'total_active_ram_size', 'total_active_vcpus', 'total_cpu_usage',
             'total_disk_usage', 'total_hours', 'total_ram_usage']


class User(APIResourceWrapper):
    """Simple wrapper around openstackx.extras.users.User"""
    _attrs = ['email', 'enabled', 'id', 'tenantId', 'name']


class Role(APIResourceWrapper):
    """Wrapper around user role"""
    _attrs = ['id', 'name', 'description', 'service_id']


class SecurityGroup(APIResourceWrapper):
    """Simple wrapper around openstackx.extras.security_groups.SecurityGroup"""
    _attrs = ['id', 'name', 'description', 'tenant_id', 'rules']


class SecurityGroupRule(APIResourceWrapper):
    """Simple wrapper around openstackx.extras.security_groups.SecurityGroupRule"""
    _attrs = ['id', 'parent_group_id', 'group_id', 'ip_protocol',
              'from_port', 'to_port', 'groups', 'ip_ranges']


class SecurityGroupRule(APIResourceWrapper):
    """Simple wrapper around openstackx.extras.users.User"""
    _attrs = ['id', 'name', 'description', 'tenant_id', 'security_group_rules']


class SwiftAuthentication(object):
    """Auth container to pass CloudFiles storage URL and token from
       session.
    """
    def __init__(self, storage_url, auth_token):
        self.storage_url = storage_url
        self.auth_token = auth_token

    def authenticate(self):
        return (self.storage_url, '', self.auth_token)


class ServiceCatalogException(api_exceptions.ApiException):
    def __init__(self, service_name):
        message = 'Invalid service catalog service: %s' % service_name
        super(ServiceCatalogException, self).__init__(404, message)


class VirtualInterface(APIResourceWrapper):
    _attrs = ['id', 'mac_address']


def get_service_from_catalog(catalog, service_type):
    for service in catalog:
        if service['type'] == service_type:
            return service
    return None


def url_for(request, service_type, admin=False):
    catalog = request.user.service_catalog
    service = get_service_from_catalog(catalog, service_type)
    if service:
        try:
            if admin:
                return service['endpoints'][0]['adminURL']
            else:
                return service['endpoints'][0]['internalURL']
        except (IndexError, KeyError):
            raise ServiceCatalogException(service_type)
    else:
        raise ServiceCatalogException(service_type)


def check_openstackx(f):
    """Decorator that adds extra info to api exceptions

       The OpenStack Dashboard currently depends on openstackx extensions
       being present in Nova.  Error messages depending for views depending
       on these extensions do not lead to the conclusion that Nova is missing
       extensions.

       This decorator should be dropped and removed after Keystone and
       Horizon more gracefully handle extensions and openstackx extensions
       aren't required by Horizon in Nova.
    """
    def inner(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except api_exceptions.NotFound, e:
            e.message = e.details or ''
            e.message += ' This error may be caused by a misconfigured' \
                         ' Nova url in keystone\'s service catalog, or ' \
                         ' by missing openstackx extensions in Nova. ' \
                         ' See the Horizon README.'
            raise

    return inner


def compute_api(request):
    compute = openstack.compute.Compute(
        auth_token=request.user.token,
        management_url=url_for(request, 'compute'))
    # this below hack is necessary to make the jacobian compute client work
    # TODO(mgius): It looks like this is unused now?
    compute.client.auth_token = request.user.token
    compute.client.management_url = url_for(request, 'compute')
    LOG.debug('compute_api connection created using token "%s"'
                      ' and url "%s"' %
                      (request.user.token, url_for(request, 'compute')))
    return compute


def account_api(request):
    LOG.debug('account_api connection created using token "%s"'
                      ' and url "%s"' %
                  (request.user.token,
                   url_for(request, 'identity', True)))
    return openstackx.extras.Account(
        auth_token=request.user.token,
        management_url=url_for(request, 'identity', True))


def glance_api(request):
    o = urlparse.urlparse(url_for(request, 'image'))
    LOG.debug('glance_api connection created for host "%s:%d"' %
                     (o.hostname, o.port))
    return glance_client.Client(o.hostname, o.port, auth_tok=request.user.token)


def admin_api(request):
    LOG.debug('admin_api connection created using token "%s"'
                    ' and url "%s"' %
                    (request.user.token, url_for(request, 'compute', True)))
    return openstackx.admin.Admin(auth_token=request.user.token,
                            management_url=url_for(request, 'compute', True))


def extras_api(request):
    LOG.debug('extras_api connection created using token "%s"'
                     ' and url "%s"' %
                    (request.user.token, url_for(request, 'compute')))
    return openstackx.extras.Extras(auth_token=request.user.token,
                                   management_url=url_for(request, 'compute'))


def _get_base_client_from_token(tenant_id, token):
    '''
    Helper function to create an instance of novaclient.client.HTTPClient from
    a token and tenant id rather than a username/password.

    The returned client can be passed to novaclient.keystone.client.Client
    without requiring a second authentication call.

    NOTE(gabriel): This ought to live upstream in novaclient, but isn't
    currently supported by the HTTPClient.authenticate() method (which only
    works with a username and password).
    '''
    c = base_nova_client.HTTPClient(None, None, tenant_id,
                                settings.OPENSTACK_KEYSTONE_URL)
    body = {"auth": {"tenantId": tenant_id, "token": {"id": token}}}
    token_url = urlparse.urljoin(c.auth_url, "tokens")
    resp, body = c.request(token_url, "POST", body=body)
    c._extract_service_catalog(c.auth_url, resp, body)
    return c


def novaclient(request):
    LOG.debug('novaclient connection created using token "%s" and url "%s"' %
              (request.user.token, url_for(request, 'compute')))
    c = nova_client.Client(username=request.user.username,
                      api_key=request.user.token,
                      project_id=request.user.tenant_id,
                      auth_url=url_for(request, 'compute'))
    c.client.auth_token = request.user.token
    c.client.management_url = url_for(request, 'compute')
    return c


def auth_api():
    LOG.debug('auth_api connection created using url "%s"' %
                   settings.OPENSTACK_KEYSTONE_URL)
    return openstackx.auth.Auth(
            management_url=settings.OPENSTACK_KEYSTONE_URL)


def swift_api(request):
    LOG.debug('object store connection created using token "%s"'
                ' and url "%s"' %
                (request.session['token'], url_for(request, 'object-store')))
    auth = SwiftAuthentication(url_for(request, 'object-store'),
                               request.session['token'])
    return cloudfiles.get_connection(auth=auth)


def quantum_api(request):
    tenant = None
    if hasattr(request, 'user'):
        tenant = request.user.tenant_id
    else:
        tenant = settings.QUANTUM_TENANT

    return quantum_client.Client(settings.QUANTUM_URL, settings.QUANTUM_PORT,
                  False, tenant, 'json')


def console_create(request, instance_id, kind='text'):
    return Console(extras_api(request).consoles.create(instance_id, kind))


def flavor_create(request, name, memory, vcpu, disk, flavor_id):
    # TODO -- convert to novaclient when novaclient adds create support
    return Flavor(admin_api(request).flavors.create(
            name, int(memory), int(vcpu), int(disk), flavor_id))


def flavor_delete(request, flavor_id, purge=False):
    # TODO -- convert to novaclient when novaclient adds delete support
    admin_api(request).flavors.delete(flavor_id, purge)


def flavor_get(request, flavor_id):
    return Flavor(novaclient(request).flavors.get(flavor_id))


def flavor_list(request):
    return [Flavor(f) for f in novaclient(request).flavors.list()]


def tenant_floating_ip_list(request):
    """
    Fetches a list of all floating ips.
    """
    return [FloatingIp(ip) for ip in novaclient(request).floating_ips.list()]


def tenant_floating_ip_get(request, floating_ip_id):
    """
    Fetches a floating ip.
    """
    return novaclient(request).floating_ips.get(floating_ip_id)


def tenant_floating_ip_allocate(request):
    """
    Allocates a floating ip to tenant.
    """
    return novaclient(request).floating_ips.create()


def tenant_floating_ip_release(request, floating_ip_id):
    """
    Releases floating ip from the pool of a tenant.
    """
    return novaclient(request).floating_ips.delete(floating_ip_id)


def image_create(request, image_meta, image_file):
    return Image(glance_api(request).add_image(image_meta, image_file))


def image_delete(request, image_id):
    return glance_api(request).delete_image(image_id)


def image_get(request, image_id):
    return Image(glance_api(request).get_image(image_id)[0])


def image_list_detailed(request):
    return [Image(i) for i in glance_api(request).get_images_detailed()]


def snapshot_list_detailed(request):
    filters = {}
    filters['property-image_type'] = 'snapshot'
    filters['is_public'] = 'none'
    return [Image(i) for i in glance_api(request)
                             .get_images_detailed(filters=filters)]


def snapshot_create(request, instance_id, name):
    return novaclient(request).servers.create_image(instance_id, name)


def image_update(request, image_id, image_meta=None):
    image_meta = image_meta and image_meta or {}
    return Image(glance_api(request).update_image(image_id,
                                                  image_meta=image_meta))


def keypair_create(request, name):
    return KeyPair(novaclient(request).keypairs.create(name))


def keypair_import(request, name, public_key):
    return KeyPair(novaclient(request).keypairs.create(name, public_key))


def keypair_delete(request, keypair_id):
    novaclient(request).keypairs.delete(keypair_id)


def keypair_list(request):
    return [KeyPair(key) for key in novaclient(request).keypairs.list()]


def server_create(request, name, image, flavor,
                           key_name, user_data, security_groups):
    return Server(novaclient(request).servers.create(
            name, image, flavor, userdata=user_data,
            security_groups=security_groups,
            key_name=key_name), request)


def server_delete(request, instance):
    compute_api(request).servers.delete(instance)


def server_get(request, instance_id):
    return Server(extras_api(request).servers.get(instance_id), request)


@check_openstackx
def server_list(request):
    return [Server(s, request) for s in extras_api(request).servers.list()]


@check_openstackx
def admin_server_list(request):
    return [Server(s, request) for s in admin_api(request).servers.list()]


def server_reboot(request,
                  instance_id,
                  hardness=openstack.compute.servers.REBOOT_HARD):
    server = server_get(request, instance_id)
    server.reboot(hardness)


def server_update(request, instance_id, name, description):
    return extras_api(request).servers.update(instance_id,
                                              name=name,
                                              description=description)


def server_add_floating_ip(request, server, address):
    """
    Associates floating IP to server's fixed IP.
    """
    server = novaclient(request).servers.get(server)
    fip = novaclient(request).floating_ips.get(address)

    return novaclient(request).servers.add_floating_ip(server, fip)


def server_remove_floating_ip(request, server, address):
    """
    Removes relationship between floating and server's fixed ip.
    """
    fip = novaclient(request).floating_ips.get(address)
    server = novaclient(request).servers.get(fip.instance_id)

    return novaclient(request).servers.remove_floating_ip(server, fip)


def service_get(request, name):
    return Services(admin_api(request).services.get(name))


@check_openstackx
def service_list(request):
    return [Services(s) for s in admin_api(request).services.list()]


def service_update(request, name, enabled):
    return Services(admin_api(request).services.update(name, enabled))


def token_get_tenant(request, tenant_id):
    tenants = auth_api().tenants.for_token(request.user.token)
    for t in tenants:
        if str(t.id) == str(tenant_id):
            return Tenant(t)

    LOG.warning('Unknown tenant id "%s" requested' % tenant_id)


def token_list_tenants(request, token):
    return [Tenant(t) for t in auth_api().tenants.for_token(token)]


def tenant_create(request, tenant_name, description, enabled):
    return Tenant(account_api(request).tenants.create(tenant_name,
                                                      description,
                                                      enabled))


def tenant_get(request, tenant_id):
    return Tenant(account_api(request).tenants.get(tenant_id))


def tenant_delete(request, tenant_id):
    account_api(request).tenants.delete(tenant_id)


@check_openstackx
def tenant_list(request):
    return [Tenant(t) for t in account_api(request).tenants.list()]


def tenant_list_for_token(request, token):
    # FIXME: use novaclient for this
    keystone = openstackx.auth.Auth(
                            management_url=settings.OPENSTACK_KEYSTONE_URL)
    return [Tenant(t) for t in keystone.tenants.for_token(token)]


def users_list_for_token_and_tenant(request, token, tenant):
    admin_account = openstackx.extras.Account(
                    auth_token=token,
                    management_url=settings.OPENSTACK_KEYSTONE_ADMIN_URL)
    return [User(u) for u in admin_account.users.get_for_tenant(tenant)]


def tenant_update(request, tenant_id, tenant_name, description, enabled):
    return Tenant(account_api(request).tenants.update(tenant_id,
                                                      tenant_name,
                                                      description,
                                                      enabled))


def token_create(request, tenant, username, password):
    '''
    Creates a token using the username and password provided. If tenant
    is provided it will retrieve a scoped token and the service catalog for
    the given tenant. Otherwise it will return an unscoped token and without
    a service catalog.
    '''
    c = base_nova_client.HTTPClient(username, password, tenant,
                                settings.OPENSTACK_KEYSTONE_URL)
    c.version = 'v2.0'
    try:
        c.authenticate()
    except nova_exceptions.AuthorizationFailure as e:
        # When authenticating without a tenant, novaclient raises a KeyError
        # (which is caught and raised again as an AuthorizationFailure)
        # if no service catalog is returned. However, in this case if we got
        # back a token we're good. If not then it really is a failure.
        if c.service_catalog.get_token():
            pass
        else:
            raise
    access = c.service_catalog.catalog['access']
    return Token(id=c.auth_token,
                 serviceCatalog=access.get('serviceCatalog', None),
                 user=access['user'],
                 tenant_id=tenant)

def token_create_scoped(request, tenant, token):
    '''
    Creates a scoped token using the tenant id and unscoped token; retrieves
    the service catalog for the given tenant.
    '''
    c = _get_base_client_from_token(tenant, token)
    access = c.service_catalog.catalog['access']
    return Token(id=c.auth_token,
                 serviceCatalog=access.get('serviceCatalog', None),
                 user=access['user'],
                 tenant_id=tenant)

def tenant_quota_get(request, tenant):
    return novaclient(request).quotas.get(tenant)


@check_openstackx
def usage_get(request, tenant_id, start, end):
    return Usage(extras_api(request).usage.get(tenant_id, start, end))


@check_openstackx
def usage_list(request, start, end):
    return [Usage(u) for u in extras_api(request).usage.list(start, end)]


def user_create(request, user_id, email, password, tenant_id, enabled):
    return User(account_api(request).users.create(
            user_id, email, password, tenant_id, enabled))


def user_delete(request, user_id):
    account_api(request).users.delete(user_id)


def user_get(request, user_id):
    return User(account_api(request).users.get(user_id))


def security_group_list(request):
    return [SecurityGroup(g) for g in novaclient(request).\
                                     security_groups.list()]


def security_group_get(request, security_group_id):
    return SecurityGroup(novaclient(request).\
                         security_groups.get(security_group_id))


def security_group_create(request, name, description):
    return SecurityGroup(novaclient(request).\
                         security_groups.create(name, description))


def security_group_delete(request, security_group_id):
    novaclient(request).security_groups.delete(security_group_id)


def security_group_rule_create(request, parent_group_id, ip_protocol=None,
                               from_port=None, to_port=None, cidr=None,
                               group_id=None):
    return SecurityGroup(novaclient(request).\
                         security_group_rules.create(parent_group_id,
                                                     ip_protocol,
                                                     from_port,
                                                     to_port,
                                                     cidr,
                                                     group_id))


def security_group_rule_delete(request, security_group_rule_id):
    novaclient(request).security_group_rules.delete(security_group_rule_id)


@check_openstackx
def user_list(request):
    return [User(u) for u in account_api(request).users.list()]


def user_update_email(request, user_id, email):
    return User(account_api(request).users.update_email(user_id, email))


def user_update_enabled(request, user_id, enabled):
    return User(account_api(request).users.update_enabled(user_id, enabled))


def user_update_password(request, user_id, password):
    return User(account_api(request).users.update_password(user_id, password))


def user_update_tenant(request, user_id, tenant_id):
    return User(account_api(request).users.update_tenant(user_id, tenant_id))


def _get_role(request, name):
    roles = account_api(request).roles.list()
    for role in roles:
        if role.name.lower() == name.lower():
            return role

    raise Exception('Role does not exist: %s' % name)


def role_add_for_tenant_user(request, tenant_id, user_id, role_name):
    role = _get_role(request, role_name)
    account_api(request).role_refs.add_for_tenant_user(
                tenant_id,
                user_id,
                role.id)


def role_delete_for_tenant_user(request, tenant_id, user_id, role_name):
    role = _get_role(request, role_name)
    account_api(request).role_refs.delete_for_tenant_user(
                tenant_id,
                user_id,
                role.id)


def swift_container_exists(request, container_name):
    try:
        swift_api(request).get_container(container_name)
        return True
    except cloudfiles.errors.NoSuchContainer:
        return False


def swift_object_exists(request, container_name, object_name):
    container = swift_api(request).get_container(container_name)

    try:
        container.get_object(object_name)
        return True
    except cloudfiles.errors.NoSuchObject:
        return False


def swift_get_containers(request, marker=None):
    return [Container(c) for c in swift_api(request).get_all_containers(
                    limit=getattr(settings, 'SWIFT_PAGINATE_LIMIT', 10000),
                    marker=marker)]


def swift_create_container(request, name):
    if swift_container_exists(request, name):
        raise Exception('Container with name %s already exists.' % (name))

    return Container(swift_api(request).create_container(name))


def swift_delete_container(request, name):
    swift_api(request).delete_container(name)


def swift_get_objects(request, container_name, prefix=None, marker=None):
    container = swift_api(request).get_container(container_name)
    objects = container.get_objects(prefix=prefix, marker=marker,
                limit=getattr(settings, 'SWIFT_PAGINATE_LIMIT', 10000))
    return [SwiftObject(o) for o in objects]


def swift_copy_object(request, orig_container_name, orig_object_name,
                      new_container_name, new_object_name):

    container = swift_api(request).get_container(orig_container_name)

    if swift_object_exists(request,
                           new_container_name,
                           new_object_name) == True:
        raise Exception('Object with name %s already exists in container %s'
        % (new_object_name, new_container_name))

    orig_obj = container.get_object(orig_object_name)
    return orig_obj.copy_to(new_container_name, new_object_name)


def swift_upload_object(request, container_name, object_name, object_data):
    container = swift_api(request).get_container(container_name)
    obj = container.create_object(object_name)
    obj.write(object_data)


def swift_delete_object(request, container_name, object_name):
    container = swift_api(request).get_container(container_name)
    container.delete_object(object_name)


def swift_get_object_data(request, container_name, object_name):
    container = swift_api(request).get_container(container_name)
    return container.get_object(object_name).stream()


def quantum_list_networks(request):
    return quantum_api(request).list_networks()


def quantum_network_details(request, network_id):
    return quantum_api(request).show_network_details(network_id)


def quantum_list_ports(request, network_id):
    return quantum_api(request).list_ports(network_id)


def quantum_port_details(request, network_id, port_id):
    return quantum_api(request).show_port_details(network_id, port_id)


def quantum_create_network(request, data):
    return quantum_api(request).create_network(data)


def quantum_delete_network(request, network_id):
    return quantum_api(request).delete_network(network_id)


def quantum_update_network(request, network_id, data):
    return quantum_api(request).update_network(network_id, data)


def quantum_create_port(request, network_id):
    return quantum_api(request).create_port(network_id)


def quantum_delete_port(request, network_id, port_id):
    return quantum_api(request).delete_port(network_id, port_id)


def quantum_attach_port(request, network_id, port_id, data):
    return quantum_api(request).attach_resource(network_id, port_id, data)


def quantum_detach_port(request, network_id, port_id):
    return quantum_api(request).detach_resource(network_id, port_id)


def quantum_set_port_state(request, network_id, port_id, data):
    return quantum_api(request).set_port_state(network_id, port_id, data)


def quantum_port_attachment(request, network_id, port_id):
    return quantum_api(request).show_port_attachment(network_id, port_id)


def get_vif_ids(request):
    vifs = []
    attached_vifs = []
    # Get a list of all networks
    networks_list = quantum_api(request).list_networks()
    for network in networks_list['networks']:
        ports = quantum_api(request).list_ports(network['id'])
        # Get port attachments
        for port in ports['ports']:
            port_attachment = quantum_api(request).show_port_attachment(
                                                    network['id'],
                                                    port['id'])
            if port_attachment['attachment']:
                attached_vifs.append(
                    port_attachment['attachment']['id'].encode('ascii'))
    # Get all instances
    instances = server_list(request)
    # Get virtual interface ids by instance
    for instance in instances:
        id = instance.id
        instance_vifs = extras_api(request).virtual_interfaces.list(id)
        for vif in instance_vifs:
            # Check if this VIF is already connected to any port
            if str(vif.id) in attached_vifs:
                vifs.append({
                    'id': vif.id,
                    'instance': instance.id,
                    'instance_name': instance.name,
                    'available': False
                })
            else:
                vifs.append({
                    'id': vif.id,
                    'instance': instance.id,
                    'instance_name': instance.name,
                    'available': True
                })
    return vifs


class GlobalSummary(object):
    node_resources = ['vcpus', 'disk_size', 'ram_size']
    unit_mem_size = {'disk_size': ['GiB', 'TiB'], 'ram_size': ['MiB', 'GiB']}
    node_resource_info = ['', 'active_', 'avail_']

    def __init__(self, request):
        self.summary = {}
        for rsrc in GlobalSummary.node_resources:
            for info in GlobalSummary.node_resource_info:
                self.summary['total_' + info + rsrc] = 0
        self.request = request
        self.service_list = []
        self.usage_list = []

    def service(self):
        try:
            self.service_list = service_list(self.request)
        except api_exceptions.ApiException, e:
            self.service_list = []
            LOG.exception('ApiException fetching service list in instance usage')
            messages.error(self.request,
                           _('Unable to get service info: %s') % e.message)
            return

        for service in self.service_list:
            if service.type == 'nova-compute':
                self.summary['total_vcpus'] += min(service.stats['max_vcpus'],
                        service.stats.get('vcpus', 0))
                self.summary['total_disk_size'] += min(
                        service.stats['max_gigabytes'],
                        service.stats.get('local_gb', 0))
                self.summary['total_ram_size'] += min(
                        service.stats['max_ram'],
                        service.stats['memory_mb']) if 'max_ram' \
                                in service.stats \
                                else service.stats.get('memory_mb', 0)

    def usage(self, datetime_start, datetime_end):
        try:
            self.usage_list = usage_list(self.request, datetime_start,
                    datetime_end)
        except api_exceptions.ApiException, e:
            self.usage_list = []
            LOG.exception('ApiException fetching usage list in instance usage'
                      ' on date range "%s to %s"' % (datetime_start,
                                                     datetime_end))
            messages.error(self.request,
                    _('Unable to get usage info: %s') % e.message)
            return

        for usage in self.usage_list:
            # FIXME: api needs a simpler dict interface (with iteration)
            # - anthony
            # NOTE(mgius): Changed this on the api end.  Not too much
            # neater, but at least its not going into private member
            # data of an external class anymore
            # usage = usage._info
            for k in usage._attrs:
                v = usage.__getattr__(k)
                if type(v) in [float, int]:
                    if not k in self.summary:
                        self.summary[k] = 0
                    self.summary[k] += v

    def human_readable(self, rsrc):
        if self.summary['total_' + rsrc] > 1023:
            self.summary['unit_' + rsrc] = GlobalSummary.unit_mem_size[rsrc][1]
            mult = 1024.0
        else:
            self.summary['unit_' + rsrc] = GlobalSummary.unit_mem_size[rsrc][0]
            mult = 1.0

        for kind in GlobalSummary.node_resource_info:
            self.summary['total_' + kind + rsrc + '_hr'] = \
                    self.summary['total_' + kind + rsrc] / mult

    def avail(self):
        for rsrc in GlobalSummary.node_resources:
            self.summary['total_avail_' + rsrc] = \
                    self.summary['total_' + rsrc] - \
                    self.summary['total_active_' + rsrc]
