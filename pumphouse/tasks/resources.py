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

import logging

from taskflow.patterns import graph_flow, unordered_flow

from pumphouse.tasks import server_resources


LOG = logging.getLogger(__name__)


def migrate_resources(src, dst, store, tenant_id):
    tenant = src.keystone.tenants.get(tenant_id)
    restricted_src = src.restrict(tenant_name=tenant.name)
    servers = restricted_src.nova.servers.list()
    flow = graph_flow.Flow("migrate-resources-{}".format(tenant_id))
    servers_flow = unordered_flow.Flow("migrate-servers-{}".format(tenant_id))
    migrate_server = server_resources.migrate_server.select("image")
    for server in servers:
        server_binding = "server-{}".format(server.id)
        if server_binding not in store:
            user = src.keystone.users.get(server.user_id)
            restricted_dst = dst.restrict(username=user.name,
                                          tenant_name=tenant.name,
                                          password="default")
            resources, server_flow, store = migrate_server(src, dst,
                                                           restricted_src,
                                                           restricted_dst,
                                                           store,
                                                           server.id)
            flow.add(*resources)
            servers_flow.add(server_flow)
    flow.add(servers_flow)
    return flow, store
