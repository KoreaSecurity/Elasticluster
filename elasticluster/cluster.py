#! /usr/bin/env python
#
# Copyright (C) 2013 GC3, University of Zurich
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
__author__ = 'Nicolas Baer <nicolas.baer@uzh.ch>, Antonio Messina <antonio.s.messina@gmail.com>'

# System imports
import operator
import os
import re
import signal
import socket
import sys
import time
from multiprocessing.dummy import Pool

# External modules
import paramiko
from binascii import hexlify

# Elasticluster imports
from elasticluster import log
from elasticluster.exceptions import TimeoutError, NodeNotFound, \
    InstanceError, ClusterError
from elasticluster.repository import MemRepository

class IgnorePolicy(paramiko.MissingHostKeyPolicy):
    def missing_host_key(self, client, hostname, key):
        log.info('Ignoring unknown %s host key for %s: %s' %
                 (key.get_name(), hostname, hexlify(key.get_fingerprint())))


class Cluster(object):
    """This is the heart of elasticluster and handles all cluster relevant
    behavior. You can basically start, setup and stop a cluster. Also it
    provides factory methods to add nodes to the cluster.
    A typical workflow is as follows:

    * create a new cluster
    * add nodes to fit your computing needs
    * start cluster; start all instances in the cloud
    * setup cluster; configure all nodes to fit your computing cluster
    * eventually stop cluster; destroys all instances in the cloud

    :param str name: unique identifier of the cluster

    :param cloud_provider: access to the cloud to manage nodes
    :type cloud_provider: :py:class:`elasticluster.providers.AbstractCloudProvider`

    :param setup_provider: provider to setup cluster
    :type setup_provider: :py:class:`elasticluster.providers.AbstractSetupProvider`

    :param str user_key_name: name of the ssh key to connect to cloud

    :param str user_key_public: path to ssh public key file

    :param str user_key_private: path to ssh private key file

    :param repository: by default the
                       :py:class:`elasticluster.repository.MemRepository` is
                       used to store the cluster in memory. Provide another
                       repository to store the cluster in a persistent state.
    :type repository: :py:class:`elasticluster.repository.AbstractClusterRepository`

    :param extra: tbd.


    :ivar nodes: dict [node_type] = [:py:class:`Node`] that represents all
                 nodes in this cluster
   """
    startup_timeout = 60 * 10  #: timeout in seconds to start all nodes

    def __init__(self, name, cloud_provider, setup_provider,
                 user_key_name, user_key_public,
                 user_key_private, repository=None, **extra):
        self.name = name
        self._cloud_provider = cloud_provider
        self._setup_provider = setup_provider
        self._user_key_name = user_key_name
        self._user_key_public = user_key_public
        self.user_key_private = user_key_private
        self.repository = repository if repository else MemRepository()
        self.ssh_to = extra.get('ssh_to')
        self.extra = extra.copy()
        self.nodes = dict()

        self.user_key_private = os.path.expandvars(self.user_key_private)
        self.user_key_private = os.path.expanduser(self.user_key_private)

        self._user_key_public = os.path.expanduser(self._user_key_public)
        self._user_key_public = os.path.expandvars(self._user_key_public)


    def add_node(self, kind, image_id, image_user, flavor,
                 security_group, image_userdata='', name=None):
        """Adds a new node to the cluster. This factory method provides an
        easy way to add a new node to the cluster by specifying all relevant
        parameters. The node does not get started nor setup automatically,
        this has to be done manually afterwards.

        :param str kind: kind of node to start. this refers to the
                         groups defined in the ansible setup provider
                         :py:class:`elasticluster.providers.AnsibleSetupProvider`
                         Please note that this must match the
                         `[a-zA-Z0-9-]` regexp, as it is used to build
                         a valid hostname

        :param str image_id: image id to use for the cloud instance (e.g.
                             ami on amazon)

        :param str image_user: user to login on given image

        :param str flavor: machine type to use for cloud instance

        :param str security_group: security group that defines firewall rules
                                   to the instance

        :param str image_userdata: commands to execute after instance starts

        :param str name: name of this node, automatically generated if None

        :raises: ValueError: `kind` argument is an invalid string.

        :return: created :py:class:`Node`

        """
        if not re.match("^[a-zA-Z0-9-]+$", kind):
            raise ValueError(
                "Invalid name `%s`. The `kind` argument may only contains "
                "characters in [a-z0-9-] range, as it is going to be used as "
                "hostname" % kind
            )

        if kind not in self.nodes:
            self.nodes[kind] = []

        if not name:
            name = "%s%03d" % (kind, len(self.nodes[kind]) + 1)

        node = Node(name, kind, self._cloud_provider,
                    self._user_key_public, self.user_key_private,
                    self._user_key_name, image_user, security_group,
                    image_id, flavor, image_userdata=image_userdata)

        self.nodes[kind].append(node)
        return node

    def add_nodes(self, kind, num, image_id, image_user, flavor,
                  security_group, image_userdata=''):
        """Helper method to add multiple nodes of the same kind to a cluster.

        :param str kind: kind of node to start. this refers to the groups
                         defined in the ansible setup provider
                         :py:class:`elasticluster.providers.AnsibleSetupProvider`

        :param int num: number of nodes to add of this kind

        :param str image_id: image id to use for the cloud instance (e.g.
                             ami on amazon)

        :param str image_user: user to login on given image

        :param str flavor: machine type to use for cloud instance

        :param str security_group: security group that defines firewall rules
                                   to the instance

        :param str image_userdata: commands to execute after instance starts
        """
        for i in range(num):
            self.add_node(kind, image_id, image_user, flavor,
                          security_group, image_userdata=image_userdata)

    def remove_node(self, node):
        """Removes a node from the cluster, but does not stop it. Use this
        method with caution.

        :param node: node to remove
        :type node: :py:class:`Node`
        """
        if node.kind not in self.nodes:
            log.error("Unable to remove node %s: invalid node type `%s`.",
                      node.name, node.kind)
        else:
            index = self.nodes[node.kind].index(node)
            if self.nodes[node.kind][index]:
                del self.nodes[node.kind][index]

    @staticmethod
    def _start_node(node):
        """Static method to start a specific node on a cloud

        :return: bool -- True on success, False otherwise
        """
        log.debug("_start_node: working on node %s" % node.name)
        # TODO: the following check is not optimal yet. When a
        # node is still in a starting state,
        # it will start another node here,
        # since the `is_alive` method will only check for
        # running nodes (see issue #13)
        if node.is_alive():
            log.info("Not starting node %s which is "
                     "already up&running.", node.name)
            return True
        else:
            try:
                node.start()
                log.info("_start_node: node has been started")
                return True
            except Exception as e:
                log.error("could not start node `%s` for reason "
                          "`%s`" % (node.name, e))
                return None

    def start(self, min_nodes=None):
        """Starts up all the instances in the cloud. To speed things up all
        instances are started in a seperate thread. To make sure
        elasticluster is not stopped during creation of an instance, it will
        overwrite the sigint handler. As soon as the last started instance
        is returned and saved to the repository, sigint is executed as usual.
        An instance is up and running as soon as a ssh connection can be
        established. If the startup timeout is reached before all instances
        are started, the cluster will stop and destroy all instances.

        This method is blocking and might take some time depending on the
        amount of instances to start.

        :param min_nodes: minimum number of nodes to start in case the quota
                          is reached before all instances are up
        :type min_nodes: dict [node_kind] = number
        """

        # To not mess up the cluster management we start the nodes in a
        # different thread. In this case the main thread receives the sigint
        # and communicates to the `start_node` thread. The nodes to work on
        # are passed in a managed queue.
        self.keep_running = True

        def sigint_handler(signal, frame):
            """
            Makes sure the cluster is stored, before the sigint results in
            exiting during the node startup.
            """
            log.error("user interruption: saving cluster before exit.")
            self.keep_running = False

        nodes = self.get_all_nodes()
        thread_pool = Pool(processes=len(nodes))
        log.debug("Created pool of %d threads" % len(nodes))
        signal.signal(signal.SIGINT, sigint_handler)

        # This is blocking
        result = thread_pool.map_async(self._start_node, nodes)

        while not result.ready():
            result.wait(1)
            if not self.keep_running:
                # the user did abort the start of the cluster. We finish the
                #  current start of a node and save the status to the
                # storage, so we don't have not managed instances laying
                # around
                log.error("Aborting upon Ctrl-C")
                thread_pool.close()
                thread_pool.join()
                self.repository.save_or_update(self)
                sys.exit(1)

        # dump the cluster here, so we don't loose any knowledge
        self.repository.save_or_update(self)

        signal.alarm(0)

        def sigint_reset(signal, frame):
            sys.exit(1)
        signal.signal(signal.SIGINT, sigint_reset)

        # check if all nodes are running, stop all nodes if the
        # timeout is reached
        def timeout_handler(signum, frame):
            raise TimeoutError("problems occured while starting the nodes, "
                               "timeout `%i`", Cluster.startup_timeout)

        signal.signal(signal.SIGALRM, timeout_handler)
        signal.alarm(Cluster.startup_timeout)

        starting_nodes = self.get_all_nodes()
        try:
            while starting_nodes:
                starting_nodes = [n for n in starting_nodes
                                  if not n.is_alive()]
                if starting_nodes:
                    time.sleep(10)
        except TimeoutError as timeout:
            log.error("Not all nodes were started correctly within the given"
                      " timeout `%s`" % Cluster.startup_timeout)
            for node in starting_nodes:
                log.error("Stopping node `%s`, since it could not start "
                          "within the given timeout" % node.name)
                node.stop()
                self.remove_node(node)

        signal.alarm(0)

        # If we reached this point, we should have IP addresses for
        # the nodes, so update the storage file again.
        self.repository.save_or_update(self)

        # Try to connect to each node. Run the setup action only when
        # we successfully connect to all of them.
        signal.signal(signal.SIGALRM, timeout_handler)
        signal.alarm(Cluster.startup_timeout)
        pending_nodes = self.get_all_nodes()[:]

        try:
            while pending_nodes:
                for node in pending_nodes[:]:
                    if node.connect():
                        log.info("Connection to node %s (%s) successful.",
                                 node.name, node.connection_ip())
                        pending_nodes.remove(node)
                if pending_nodes:
                    time.sleep(5)

        except TimeoutError:
            # remove the pending nodes from the cluster
            log.error("Could not connect to all the nodes of the "
                      "cluster within the given timeout `%s`."
                      % Cluster.startup_timeout)
            for node in pending_nodes:
                log.error("Stopping node `%s`, since we could not connect to"
                          " it within the timeout." % node.name)
                node.stop()
                self.remove_node(node)

        signal.alarm(0)

        # It might be possible that the node.connect() call updated
        # the `preferred_ip` attribute, so, let's save the cluster
        # again.
        self.repository.save_or_update(self)

        # A lot of things could go wrong when starting the cluster. To
        # ensure a stable cluster fitting the needs of the user in terms of
        # cluster size, we check the minimum nodes within the node groups to
        # match the current setup.
        if not min_nodes:
            # the node minimum is implicit if not specified.
            min_nodes = dict((key, len(self.nodes[key])) for key in
                             self.nodes.iterkeys())
        else:
            # check that each group has a minimum value
            for group, nodes in nodes.iteritems():
                if group not in min_nodes:
                    min_nodes[group] = len(nodes)

        self._check_cluster_size(min_nodes)

    def _check_cluster_size(self, min_nodes):
        """Checks the size of the cluster to fit the needs of the user. It
        considers the minimum values for the node groups if present.
        Otherwise it will imply the user wants the amount of specified
        nodes at least.

        :param min_nodes: minimum number of nodes for each kind
        :type min_nodes: dict [node_kind] = number
        :raises: ClusterError in case the size does not fit the minimum
                 number specified by the user.
        """
        # check the total sizes before moving the nodes around
        minimum_nodes = 0
        for group, size in min_nodes.iteritems():
            minimum_nodes = minimum_nodes + size

        if len(self.get_all_nodes()) < minimum_nodes:
            raise ClusterError("The cluster does not provide the minimum "
                               "amount of nodes specified in the "
                               "configuration. The nodes are still running, "
                               "but will not be setup yet. Please change the"
                               " minimum amount of nodes in the "
                               "configuration or try to start a new cluster "
                               "after checking the cloud provider settings.")

        # finding all node groups with an unsatisfied amount of nodes
        unsatisfied_groups = []
        for group, size in min_nodes.iteritems():
            if len(self.nodes[group]) < size:
                unsatisfied_groups.append(group)

        # trying to move nodes around to fill the groups with missing nodes
        for ugroup in unsatisfied_groups[:]:
            missing = min_nodes[ugroup] - len(self.nodes[ugroup])
            for group, nodes in self.nodes.iteritems():
                spare = len(self.nodes[group]) - min_nodes[group]
                while spare > 0 and missing > 0:
                    self.nodes[ugroup].append(self.nodes[group][-1])
                    del self.nodes[group][-1]
                    spare = spare - 1
                    missing = missing - 1

                    if missing == 0:
                        unsatisfied_groups.remove(ugroup)

        if unsatisfied_groups:
            raise ClusterError("Could not find an optimal solution to "
                               "distribute the started nodes into the node "
                               "groups to satisfy the minimum amount of "
                               "nodes. Please change the minimum amount of "
                               "nodes in the configuration or try to start a"
                               " new clouster after checking the cloud "
                               "provider settings")

    def get_all_nodes(self):
        """Returns a list of all nodes in this cluster as a mixed list of
        different node kinds.

        :return: list of :py:class:`Node`
        """
        nodes = self.nodes.values()
        if nodes:
            return reduce(operator.add, nodes, list())
        else:
            return []

    def stop(self, force=False):
        """Destroys all instances of this cluster and calls delete on the
        repository.

        :param bool force: force termination of instances in any case
        """
        for node in self.get_all_nodes():
            if node.instance_id:
                try:
                    node.stop()
                    self.nodes[node.kind].remove(node)
                    log.debug("Removed node with instance id %s from %s"
                              % (node.instance_id, node.kind))
                except:
                    # Boto does not always raises an `Exception` class!
                    log.error("could not stop instance `%s`, it might "
                              "already be down.", node.instance_id)
            else:
                log.debug("Not stopping node with no instance id. It seems "
                          "like node `%s` did not start correctly."
                          % node.name)
                self.nodes[node.kind].remove(node)
        if not self.get_all_nodes():
            log.debug("Removing cluster %s.", self.name)
            self._setup_provider.cleanup(self)
            self.repository.delete(self)
        elif not force:
            log.warning("Not all instances have been terminated. "
                        "Please rerun the `elasticluster stop %s`", self.name)
            self.repository.save_or_update(self)
        else:
            log.warning("Not all instances have been terminated. However, "
                        "as requested, the cluster has been force-removed.")
            self._setup_provider.cleanup(self)
            self.repository.delete(self)

    def get_frontend_node(self):
        """Returns the first node of the class specified in the
        configuration file as `ssh_to`, or the first node of
        the first class in alphabetic order.

        :return: :py:class:`Node`
        :raise: :py:class:`elasticluster.exceptions.NodeNotFound` if no
                valid frontend node is found
        """
        if self.ssh_to:
            if self.ssh_to in self.nodes:
                cls = self.nodes[self.ssh_to]
                if cls:
                    return cls[0]
                else:
                    log.warning(
                        "preferred `ssh_to` `%s` is empty: unable to "
                        "get the choosen frontend node from that class.",
                        self.ssh_to)
            else:
                raise NodeNotFound(
                    "Invalid ssh_to `%s`. Please check your "
                    "configuration file." % self.ssh_to)

        # If we reach this point, the preferred class was empty. Pick
        # one using the default logic.
        for cls in sorted(self.nodes.keys()):
            if self.nodes[cls]:
                return self.nodes[cls][0]
        # Uh-oh, no nodes in this cluster.
        raise NodeNotFound("Unable to find a valid frontend: "
                           "cluster has no nodes!")

    def setup(self):
        """Configure the cluster nodes with the specified  This
        is delegated to the provided :py:class:`elasticluster.providers.AbstractSetupProvider`

        :return: bool - True on success, False otherwise
        """
        try:
            # setup the cluster using the setup provider
            ret = self._setup_provider.setup_cluster(self)
        except Exception, e:
            log.error(
                "the setup provider was not able to setup the cluster, "
                "but the cluster is running by now. Setup provider error "
                "message: `%s`", str(e))
            ret = False

        if not ret:
            log.warning(
                "Cluster `%s` not yet configured. Please, re-run "
                "`elasticluster setup %s` and/or check your configuration",
                self.name, self.name)

        return ret

    def update(self):
        """Update all connection information of the nodes of this cluster.
        It occurs for example public ip's are not available imediatly,
        therefore calling this method might help.
        """
        for node in self.get_all_nodes():
            try:
                node.update_ips()
            except InstanceError, ex:
                log.warning("Ignoring error updating information on node %s: %s",
                          node, str(ex))
        self.repository.save_or_update(self)


class Node(object):
    """The node represents an instance in a cluster. It holds all
    information to connect to the nodes also manages the cloud instance. It
    provides the basic functionality to interact with the cloud instance,
    such as start, stop, check if the instance is up and ssh connect.

    :param str name: identifier of the node

    :param str kind: kind of node in regard to cluster. this usually
                     refers to a specified group in the
                     :py:class:`elasticluster.providers.AbstractSetupProvider`

    :param cloud_provider: cloud provider to manage the instance
    :type cloud_provider: :py:class:`elasticluster.providers.AbstractCloudProvider`

    :param str user_key_public: path to the ssh public key

    :param str user_key_private: path to the ssh private key

    :param str user_key_name: name of the ssh key

    :param str image_user: user to connect to the instance via ssh

    :param str security_group: security group to setup firewall rules

    :param str image: image id to launch instance with

    :param str flavor: machine type to launch instance

    :param str image_userdata: commands to execute after instance start


    :ivar instance_id: id of the node instance on the cloud

    :ivar preferred_ip: IP address used to connect to the node.

    :ivar ips: list of all the IPs defined for this node.
    """
    connection_timeout = 5  #: timeout in seconds to connect to host via ssh

    def __init__(self, name, kind, cloud_provider, user_key_public,
                 user_key_private, user_key_name, image_user, security_group,
                 image, flavor, image_userdata=None):
        self.name = name
        self.kind = kind
        self._cloud_provider = cloud_provider
        self.user_key_public = user_key_public
        self.user_key_private = user_key_private
        self.user_key_name = user_key_name
        self.image_user = image_user
        self.security_group = security_group
        self.image = image
        self.image_userdata = image_userdata
        self.flavor = flavor

        self.instance_id = None
        self.preferred_ip = None
        self.ips = []

    def start(self):
        """Starts the node on the cloud using the given
        instance properties. This method is non-blocking, as soon
        as the node id is returned from the cloud provider, it will return.
        Therefore the `is_alive` and `update_ips` methods can be used to
        further gather details about the state of the node.
        """
        log.info("Starting node %s.", self.name)
        self.instance_id = self._cloud_provider.start_instance(
            self.user_key_name, self.user_key_public, self.user_key_private,
            self.security_group,
            self.flavor, self.image, self.image_userdata,
            username=self.image_user, node_name=self.name)
        log.debug("Node %s has instance_id: `%s`", self.name, self.instance_id)

    def stop(self):
        """Destroys the instance launched on the cloud for this specific node.
        """
        log.info("shutting down instance `%s`",
                 self.instance_id)
        self._cloud_provider.stop_instance(self.instance_id)
        # When an instance is terminated, the EC2 cloud provider will
        # basically return it as "running" state. Setting the
        # `instance_id` attribute to None will force `is_alive()`
        # method not to check with the cloud provider, and forever
        # forgetting about the instance id.
        self.instance_id = None

    def is_alive(self):
        """Checks if the current node is up and running in the cloud. It
        only checks the status provided by the cloud interface. Therefore a
        node might be running, but not yet ready to ssh into it.
        """
        running = False
        if not self.instance_id:
            return False

        try:
            log.debug("Getting information for instance %s",
                      self.instance_id)
            running = self._cloud_provider.is_instance_running(
                self.instance_id)
        except Exception, ex:
            log.debug("Ignoring error while looking for vm id %s: %s",
                      self.instance_id, str(ex))
        if running:
            log.debug("node `%s` (instance id %s) is up and running",
                      self.name, self.instance_id)
            self.update_ips()
        else:
            log.debug("node `%s` (instance id `%s`) still building...",
                      self.name, self.instance_id)

        return running

    def connection_ip(self):
        """Returns the IP to be used to connect to this node.

        If the instance has a public IP address, then this is returned, otherwise, its private IP is returned.
        """
        return self.preferred_ip

    def connect(self):
        """Connect to the node via ssh using the paramiko library.

        :return: :py:class:`paramiko.SSHClient` - ssh connection or None on
                 failure
        """
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(IgnorePolicy())
        # Try connecting using the `preferred_ip`, if
        # present. Otherwise, try all of them and set `preferred_ip`
        # using the first that is working.
        ips=self.ips[:]
        # This is done in order to "sort" the IPs and put the preferred_ip first.
        if self.preferred_ip: ips.remove(self.preferred_ip)

        for ip in [self.preferred_ip] + ips:
            try:
                log.debug("Trying to connect to host %s (%s)",
                          self.name, ip)
                ssh.connect(ip,
                            username=self.image_user,
                            allow_agent=True,
                            key_filename=self.user_key_private,
                            timeout=Node.connection_timeout)
                log.debug("Connection to %s succeded!", ip)
                if ip != self.preferred_ip:
                    log.debug("Setting `preferred_ip` to %s", ip)
                    self.preferred_ip = ip
                    cluster_changed = True
                return ssh
            except socket.error, ex:
                log.debug("Host %s (%s) not reachable: %s.",
                          self.name, ip, ex)
            except paramiko.SSHException, ex:
                log.debug("Ignoring error %s connecting to %s",
                          str(ex), self.name)

        return None

    def update_ips(self):
        """Retrieves the public and private ip of the instance by using the
        cloud provider. In some cases the public ip assignment takes some
        time, but this method is non blocking. To check for a public ip,
        consider calling this method multiple times during a certain timeout.
        """
        self.ips = self._cloud_provider.get_ips(self.instance_id)
        return self.ips[:]

    def __str__(self):
        return "name=`%s`, id=`%s`, ips=%s, "\
            "connection_ip=`%s`" % (self.name, self.instance_id, str.join(', ', self.ips),
                                    self.preferred_ip)

    def pprint(self):
        """Pretty print information about the node.

        :return: str - representaion of a node in pretty print
        """
        return """%s
connection IP: %s
IPs:    %s
instance id:   %s
instance flavor: %s""" % (self.name, self.preferred_ip, str.join(', ', self.ips),
                          self.instance_id, self.flavor)
