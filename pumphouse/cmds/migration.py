# Copyright (c) 2014 Mirantis Inc.
#
# Licensed under the Apache License, Version 2.0 (the License);
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an AS IS BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and#
# limitations under the License.

import argparse
import collections
import logging

from pumphouse import exceptions
from pumphouse import management
from pumphouse import utils
from pumphouse import flows
from pumphouse import context
from pumphouse.tasks import image as image_tasks
from pumphouse.tasks import identity as identity_tasks
from pumphouse.tasks import resources as resources_tasks

from taskflow.patterns import graph_flow


LOG = logging.getLogger(__name__)

SERVICE_TENANT_NAME = 'services'
BUILTIN_ROLES = ('service', 'admin', '_member_')


def load_cloud_driver(is_fake=False):
    if is_fake:
        import_path = "pumphouse.fake.{}"
    else:
        import_path = "pumphouse.cloud.{}"
    cloud_driver = utils.load_class(import_path.format("Cloud"))
    identity_driver = utils.load_class(import_path.format("Identity"))
    return cloud_driver, identity_driver


def get_parser():
    parser = argparse.ArgumentParser(description="Migration resources through "
                                                 "OpenStack clouds.")
    parser.add_argument("config",
                        type=utils.safe_load_yaml,
                        help="A filename of a configuration of clouds "
                             "endpoints and a strategy.")
    parser.add_argument("--fake",
                        action="store_true",
                        help="Work with FakeCloud back-end instead real "
                             "back-end from config.yaml")

    parser.add_argument("--dump",
                        default=False,
                        action="store_true",
                        help="Dump flow without execution")

    subparsers = parser.add_subparsers()
    migrate_parser = subparsers.add_parser("migrate",
                                           help="Perform a migration of "
                                                "resources from a source "
                                                "cloud to a distination.")
    migrate_parser.set_defaults(action="migrate")
    migrate_parser.add_argument("--setup",
                                action="store_true",
                                help="If present, will add test resources to "
                                     "the source cloud before starting "
                                     "migration, as 'setup' command "
                                     "would do.")
    migrate_parser.add_argument("--num-tenants",
                                default='2',
                                type=int,
                                help="Number of tenants to create on setup.")
    migrate_parser.add_argument("--num-servers",
                                default='1',
                                type=int,
                                help="Number of servers per tenant to create "
                                "on setup.")
    migrate_parser.add_argument("resource",
                                choices=RESOURCES_MIGRATIONS.keys(),
                                nargs="?",
                                default="servers",
                                help="Specify a type of resources to migrate "
                                     "to the destination cloud.")
    migrate_filter = migrate_parser.add_mutually_exclusive_group(
        required=True)
    migrate_filter.add_argument("-i", "--ids",
                                nargs="*",
                                help="A list of IDs of resource to migrate to "
                                     "the destination cloud.")
    migrate_filter.add_argument("-t", "--tenant",
                                default=None,
                                help="Specify ID of a tenant which should be "
                                     "moved to destination cloud with all "
                                     "it's resources.")
    migrate_filter.add_argument("--host",
                                default=None,
                                help="Specify hypervisor hostname to filter "
                                     "servers designated for migration.")
    cleanup_parser = subparsers.add_parser("cleanup",
                                           help="Remove resources from a "
                                                "destination cloud.")
    cleanup_parser.set_defaults(action="cleanup")
    cleanup_parser.add_argument("target",
                                nargs="?",
                                choices=("source", "destination"),
                                default="destination",
                                help="Choose a cloud to clean up.")
    setup_parser = subparsers.add_parser("setup",
                                         help="Create resource in a source "
                                              "cloud for the test purposes.")
    setup_parser.set_defaults(action="setup")
    setup_parser.add_argument("--num-tenants",
                              default='2',
                              type=int,
                              help="Number of tenants to create on setup.")
    setup_parser.add_argument("--num-servers",
                              default='1',
                              type=int,
                              help="Number of servers per tenant to create "
                              "on setup.")
    evacuate_parser = subparsers.add_parser("evacuate",
                                            help="Evacuate instances from "
                                                 "the given host.")
    evacuate_parser.set_defaults(action="evacuate")
    evacuate_parser.add_argument("host",
                                 help="The source host of the evacuation")
    return parser


def migrate_images(ctx, flow, ids):
    for image in ctx.src_cloud.glance.images.list():
        if image.id in ids:
            image_flow = image_tasks.migrate_image(
                ctx, image.id)
            flow.add(image_flow)
    return flow


def migrate_identity(ctx, flow, ids):
    for tenant_id in ids:
        _, identity_flow = identity_tasks.migrate_identity(
            ctx, tenant_id)
        flow.add(identity_flow)
    return flow


def migrate_resources(ctx, flow, ids):
    for tenant_id in ids:
        resources_flow = resources_tasks.migrate_resources(
            ctx, tenant_id)
        flow.add(resources_flow)
    return flow


def evacuate(cloud, host):
    binary = "nova-compute"
    try:
        hypervs = cloud.nova.hypervisors.search(host, servers=True)
    except exceptions.nova_excs.NotFound:
        LOG.exception("Could not find hypervisors at the host %r.", host)
    else:
        if len(hypervs) > 1:
            LOG.warning("More than one hypervisor found at the host: %s",
                        host)
        for hyperv in hypervs:
            details = cloud.nova.hypervisors.get(hyperv.id)
            host = details.service["host"]
            cloud.nova.services.disable(host, binary)
            try:
                for server in hyperv.servers:
                    cloud.nova.servers.live_migrate(server["uuid"], None,
                                                    True, False)
            except Exception:
                LOG.exception("An error occured during evacuation servers "
                              "from the host %r", host)
                cloud.nova.services.enable(host, binary)


def get_ids_by_tenant(cloud, resource_type, tenant_id):

    '''This function implements migration strategy 'tenant'

    For those types of resources that support grouping by tenant, this function
    returns a list of IDs of resources owned by the given tenant.

    :param cloud:           a collection of clients to talk to cloud services
    :param resource_type:   a type of resources designated for migration
    :param tenant_id:       an identifier of tenant that resources belong to
    :returns:               a list of IDs of resources according to passed
                            resource type
    '''

    ids = []
    if resource_type == 'users':
        ids = [user.id for user in
               cloud.keystone.users.list(tenant_id=tenant_id)]
    elif resource_type == 'images':
        ids = [image.id for image in
               cloud.glance.images.list(filters={'owner': tenant_id})]
    elif resource_type == 'servers':
        ids = [server.id for server in
               cloud.nova.servers.list(search_opts={'all_tenants': 1,
                                                    'tenant': tenant_id})]
    else:
        LOG.warn("Cannot group %s by tenant", resource_type)
    return ids


def get_ids_by_host(cloud, resource_type, hostname):

    '''Selects servers for migration based on hostname of hypervisor

    :param cloud:           a collection of clients to talk to cloud services
    :param resource_type:   a type of resources designated for migration
    :param hostname:        a name of physical servers that hosts resources
    '''

    ids = []
    if resource_type == 'servers':
        ids = [server.id for server in
               cloud.nova.servers.list(
                   search_opts={'all_tenants': 1,
                                'hypervisor_name': hostname})]
    else:
        LOG.warn("Cannot group %s by host", resource_type)
    return ids


def get_all_resource_ids(cloud, resource_type):

    '''This function implements migration strategy 'all'

    It rerurns a list of IDs of all resources of the given type in source
    cloud.

    :param cloud:            a collection of clients to talk to cloud services
    :param resource_type:    a type of resources designated for migration
    '''

    ids = []
    if resource_type == 'tenants' or resource_type == 'identity':
        ids = [tenant.id for tenant in cloud.keystone.tenants.list()]
    elif resource_type == 'roles':
        ids = [role.id for role in cloud.keystone.roles.list()]
    elif resource_type == 'users':
        ids = [user.id for user in
               cloud.keystone.users.list()]
    elif resource_type == 'images':
        ids = [image.id for image in cloud.glance.images.list()]
    elif resource_type == 'servers':
        ids = [server.id for server in
               cloud.nova.servers.list(search_opts={'all-tenants': 1})]
    elif resource_type == 'flavors':
        ids = [flavor.id for flavor in cloud.nova.flavors.list()]
    return ids


RESOURCES_MIGRATIONS = collections.OrderedDict([
    ("images", migrate_images),
    ("identity", migrate_identity),
    ("resources", migrate_resources),
])


class Events(object):
    def emit(self, *args, **kwargs):
        pass


def init_client(config, name, client_class, identity_class):
    endpoint_config = config.get("endpoint")
    identity_config = config.get("identity")
    connection = identity_config.get("connection")
    identity = identity_class(connection)
    client = client_class.from_dict(name, endpoint_config, identity)
    return client


def main():
    args = get_parser().parse_args()

    logging.basicConfig(level=logging.INFO)

    events = Events()
    flow = graph_flow.Flow("migrate-resources")
    Cloud, Identity = load_cloud_driver(is_fake=args.fake)
    config = args.config["PLUGINS"]
    if args.action == "migrate":
        store = {}
        src_config = args.config["source"]
        src = init_client(src_config,
                          "source",
                          Cloud,
                          Identity)
        if args.setup:
            workloads = args.config["source"].get("workloads", {})
            management.setup(events, src, "source", args.num_tenants,
                             args.num_servers, workloads)
        dst_config = args.config["destination"]
        dst = init_client(dst_config,
                          "destination",
                          Cloud,
                          Identity)
        migrate_function = RESOURCES_MIGRATIONS[args.resource]
        if args.ids:
            ids = args.ids
        elif args.tenant:
            ids = get_ids_by_tenant(src, args.resource, args.tenant)
        elif args.host:
            ids = get_ids_by_host(src, args.resource, args.host)
        else:
            raise exceptions.UsageError("Missing tenant ID")
        ctx = context.Context(config, src, dst)
        resources_flow = migrate_function(ctx, flow, ids)
        if (args.dump):
            # TODO custom output filename
            with open("flow.dot", "w") as f:
                utils.dump_flow(resources_flow, f, True)
            # XXX (sryabin) change to exit
            return 0

        flows.run_flow(resources_flow, ctx.store)
    elif args.action == "cleanup":
        cloud_config = args.config[args.target]
        cloud = init_client(cloud_config,
                            args.target,
                            Cloud,
                            Identity)
        management.cleanup(events, cloud, args.target)
    elif args.action == "setup":
        src_config = args.config["source"]
        src = init_client(src_config,
                          "source",
                          Cloud,
                          Identity)
        workloads = args.config["source"].get("workloads", {})
        management.setup(config, events, src, "source",
                         args.num_tenants,
                         args.num_servers,
                         workloads)
    elif args.action == "evacuate":
        cloud_config = args.config["source"]
        cloud = init_client(cloud_config,
                            "source",
                            Cloud,
                            Identity)
        evacuate(cloud, args.host)

if __name__ == "__main__":
    main()
