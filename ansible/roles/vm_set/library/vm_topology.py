#!/usr/bin/python

from contextlib import contextmanager
import functools
import hashlib
import json
import multiprocessing
import os.path
import re
import shutil
import subprocess
import shlex
import sys
import tempfile
import threading
import time
import traceback
import logging
import docker
import ipaddress
import six

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.multi_servers_utils import MultiServersUtils
from logging.handlers import MemoryHandler


try:
    from ansible.module_utils.dualtor_utils import generate_mux_cable_facts
except ImportError:
    # Add parent dir for using outside Ansible
    sys.path.append('..')
    from ansible.module_utils.dualtor_utils import generate_mux_cable_facts

from ansible.module_utils.debug_utils import config_module_logging

if sys.version_info.major == 2:
    from multiprocessing.pool import ThreadPool
else:
    from concurrent.futures import ThreadPoolExecutor as ThreadPool


DOCUMENTATION = '''
---
module: vm_topology
version_added: "0.1"
author: Pavel Shirshov (pavelsh@microsoft.com)
short_description: Create a custom virtual topology for vm_sets
description:
    - With cmd: 'create' the module:
      - creates a bridges for every VM name in vm_names which will be used for back plane connections
      - creates len(vm_names)*max_fp_num ovs bridges with name template
        "br-{{ vm_name }}-{{ 0..max_fp_num-1 }}" which will be used by FP port of VMs
    - With cmd: 'destroy' the module:
      - destroys ovs bridges which were created with 'create' cmd
    - With cmd: 'bind' the module:
      - inserts mgmt interface inside of the docker container with name "ptf_{{vm_set_name}}"
      - assigns ip address and default route to the mgmt interface
      - inserts physical vlans into the docker container to represent endhosts
      - binds internal interfaces of the docker container to corresponding VM ports
      - connects interfaces "Ethernet9" of every VM in current vm set to each other
      - connect dut fp ports to bridges representing vm set fp ports
      - connect dut mgmt ports to mgmt bridge (option)
    - with cmd: 'renumber' the module:
      - disconnect vlan interface to bridges representing vm set fp ports
      - inserts mgmt interface inside of the docker container with name "ptf_{{vm_set_name}}"
      - assigns ip address and default route to the mgmt interface
      - inserts physical vlans into the docker container to represent endhosts
      - binds internal interfaces of the docker container to corresponding VM ports
    - With cmd: 'unbind' the module:
      - destroys everything what was created with command 'bind'
    - With cmd: 'connect-vms' the module:
      - disconnect all VM ports from the DUT
    - With cmd: 'disconnect-vms' the module:
      - reconnect all VM ports to the DUT


Parameters:
    - cmd: One of the commands: 'create', 'bind', 'renumber', 'unbind', 'destroy', 'connect-vms', 'disconnect-vms'
    - vm_set_name: name of the current vm set. It will be used for generation of interface names
    - topo: dictionary with VMs topology. Check vars/topo_*.yml for details
    - vm_names: list of VMs represented on a current host
    - vm_base: which VM consider the first VM in the current vm set
    - ptf_mgmt_ip_addr: ip address with prefixlen for the injected docker container
    - ptf_mgmt_ipv6_addr: ipv6 address with prefixlen for the injected docker container
    - ptf_mgmt_ip_gw: default gateway for the injected docker container
    - ptf_mgmt_ipv6_gw: default ipv6 gateway for the injected docker container
    - ptf_extra_mgmt_ip_addr: list of ip addresses with prefixlen for the injected docker container
    - ptf_bp_ip_addr: ipv6 address with prefixlen for the injected docker container
    - ptf_bp_ipv6_addr: ipv6 address with prefixlen for the injected docker container
    - mgmt_bridge: a bridge which is used as mgmt bridge on the host
    - duts_fp_ports: duts front panel ports
    - duts_mgmt_port: duts mgmt port
    - duts_name: duts names
    - fp_mtu: MTU for FP ports
'''

EXAMPLES = '''
- name: Create VMs network
  vm_network:
    cmd:          'create'
    vm_names:     "{{ VM_hosts }}"
    fp_mtu:       "{{ fp_mtu_size }}"

- name: Bind topology {{ topo }} to VMs. base vm = {{ VM_base }}
  vm_topology:
    cmd: "bind"
    vm_set_name: "{{ vm_set_name }}"
    topo: "{{ topology }}"
    vm_names: "{{ VM_hosts }}"
    vm_base: "{{ VM_base }}"
    ptf_mgmt_ip_addr: "{{ ptf_ip }}"
    ptf_mgmt_ipv6_addr: "{{ ptf_ipv6 }}"
    ptf_mgmt_ip_gw: "{{ mgmt_gw }}"
    ptf_mgmt_ipv6_gw: "{{ mgmt_gw_v6 }}"
    ptf_extra_mgmt_ip_addr: "{{ ptf_extra_mgmt_ip }}"
    ptf_bp_ip_addr: "{{ ptf_ip }}"
    ptf_bp_ipv6_addr: "{{ ptf_ip }}"
    mgmt_bridge: "{{ mgmt_bridge }}"
    duts_mgmt_port: "{{ duts_mgmt_port }}"
    duts_fp_ports: "{{ duts_fp_ports }}"
    duts_name: "{{ duts_name }}"
    fp_mtu: "{{ fp_mtu_size }}"
    max_fp_num: "{{ max_fp_num }}

- name: Bind ptf_ip to keysight_api_server
  vm_topology:
    cmd: "bind_keysight_api_server_ip"
    ptf_mgmt_ip_addr: "{{ ptf_ip }}"
    ptf_mgmt_ipv6_addr: "{{ ptf_ipv6 }}"
    ptf_mgmt_ip_gw: "{{ mgmt_gw }}"
    ptf_mgmt_ipv6_gw: "{{ mgmt_gw_v6 | default(None) }}"
    ptf_extra_mgmt_ip_addr: "{{ ptf_extra_mgmt_ip }}"
    mgmt_bridge: "{{ mgmt_bridge }}"
    vm_names: ""
'''


DEFAULT_MTU = 0
NUM_FP_VLANS_PER_FP = 4
VM_SET_NAME_MAX_LEN = 8  # used in interface names. So restricted
MGMT_PORT_NAME = 'mgmt'
BP_PORT_NAME = 'backplane'
CMD_DEBUG_FNAME = "/tmp/vmtopology.cmds.%s.txt"

OVS_FP_BRIDGE_REGEX = 'br-%s-[0-9]+'
OVS_FP_BRIDGE_TEMPLATE = 'br-%s-%d'
OVS_FP_TAP_TEMPLATE = '%s-t%d'
OVS_BP_TAP_TEMPLATE = '%s-back'
INJECTED_INTERFACES_TEMPLATE = 'inje-%s-%d'
MUXY_INTERFACES_TEMPLATE = 'muxy-%s-%d'
ACTIVE_ACTIVE_INTERFACES_TEMPLATE = 'iaa-%s-%d'
SERVER_NIC_INTERFACE_TEMPLATE = 'nic-%s-%d'
MUXY_BRIDGE_TEMPLATE = 'mbr-%s-%d'
ACTIVE_ACTIVE_BRIDGE_TEMPLATE = 'baa-%s-%d'
NETNS_NAME_TEMPLATE = 'ns-%s'
NETNS_IFACE_TEMPLATE = 'eth%d'
PTF_NAME_TEMPLATE = 'ptf_%s'
PTF_MGMT_IF_TEMPLATE = 'ptf-%s-m'
NETNS_MGMT_IF_TEMPLATE = 'ns-%s-m'
PTF_BP_IF_TEMPLATE = 'ptf-%s-b'
ROOT_BACK_BR_TEMPLATE = 'br-b-%s'
PTF_FP_IFACE_TEMPLATE = 'eth%d'
OVS_INTERCONNECTION_BRIDGE_TEMPLATE = 'bic-%s-%s'
RETRIES = 10
# name of interface must be less than or equal to 15 bytes.
MAX_INTF_LEN = 15

VS_CHASSIS_INBAND_BRIDGE_NAME_TEMPLATE = "br-{vm_set_name}-inb"
VS_CHASSIS_MIDPLANE_BRIDGE_NAME_TEMPLATE = "br-{vm_set_name}-mid"

BACKEND_TOR_TYPE = "BackEndToRRouter"
BACKEND_LEAF_TYPE = "BackEndLeafRouter"
SUB_INTERFACE_SEPARATOR = '.'
SUB_INTERFACE_VLAN_ID = '10'

RT_TABLE_FILEPATH = "/etc/iproute2/rt_tables"

MIN_THREAD_WORKER_COUNT = 8
LOG_SEPARATOR = "=" * 120

DEFAULT_BATCH_PROCESSES_TIMEOUT = 600


def construct_log_filename(cmd, vm_set_name):
    log_filename = 'vm_topology'
    if cmd:
        log_filename += '_' + cmd
    if vm_set_name:
        log_filename += '_' + vm_set_name
    return log_filename


def adaptive_name(template, host, index):
    """
    A helper function for interface/bridge name calculation.
    Since the name of interface must be less than 15 bytes. This util is to adjust the template automatically
    according to the length of vmhost name and port index.
    The leading characters (inje, muxy, mbr) will be shorten if necessary
    e.g.
    port 21 on vms7-6 -> inje-vms7-6-21
    port 121 on vms21-1 -> inj-vms21-1-121
    port 121 on vms121-1 -> in-vms121-1-121
    """
    MAX_LEN = 15
    host_index_str = '-%s-%d' % (host, index)
    leading_len = MAX_LEN - len(host_index_str)
    leading_characters = template.split('-')[0][:leading_len]
    rendered_name = leading_characters + host_index_str
    return rendered_name


def adaptive_temporary_interface(vm_set_name, interface_name, reserved_space=0):
    """A helper function to calculate temporary interface name
    for the interface to adapt to the 15-characters name limit."""
    MAX_LEN = 15 - reserved_space
    t_suffix = "_t"
    HASH_LEN = 6
    # the max length is at least as long as the hash string length + suffix length
    if MAX_LEN < HASH_LEN + len(t_suffix):
        raise ValueError(
            "Requested length is too short to get temporary interface name.")
    interface_name_len = len(interface_name)
    ptf_name = PTF_NAME_TEMPLATE % vm_set_name
    if interface_name_len <= MAX_LEN - len(t_suffix) - HASH_LEN:
        t_int_if = hashlib.md5(ptf_name.encode(
            "utf-8")).hexdigest()[0:HASH_LEN] + interface_name + t_suffix
    else:
        t_int_if = hashlib.md5(
            (ptf_name + interface_name).encode("utf-8")).hexdigest()[0:HASH_LEN] + t_suffix
    return t_int_if


class VMTopology(object):

    def __init__(self, vm_names, vm_properties, fp_mtu, max_fp_num, topo, worker, current_vm_name=None,
                 is_dpu=False, is_vs_chassis=False, dut_interfaces=None):
        self.vm_names = vm_names
        self.current_vm_name = current_vm_name
        self.vm_properties = vm_properties
        self.fp_mtu = fp_mtu
        self.max_fp_num = max_fp_num
        self.topo = topo
        self.dut_interfaces = dut_interfaces
        self._host_interfaces = None
        self._disabled_host_interfaces = None
        self._host_interfaces_active_active = None
        self.worker = worker
        self._is_dpu = is_dpu
        self._is_vs_chassis = is_vs_chassis

    def init(self, vm_set_name, vm_base, duts_fp_ports, duts_name, ptf_exists=True, check_bridge=True):
        self.vm_set_name = vm_set_name
        self.duts_name = duts_name

        if ptf_exists:
            self.pid = VMTopology.get_pid(PTF_NAME_TEMPLATE % vm_set_name)
        else:
            self.pid = None

        self.VMs = {}
        if not self._is_dpu:
            if 'VMs' in self.topo and len(self.topo['VMs']) > 0:
                self.vm_base = vm_base
                if vm_base in self.vm_names:
                    self.vm_base_index = self.vm_names.index(vm_base)
                else:
                    raise Exception('VM_base "%s" should be presented in current vm_names: %s' % (
                        vm_base, str(self.vm_names)))
                topo_vms = self.topo['VMs']
                if self.dut_interfaces:
                    topo_vms = MultiServersUtils.get_vms_by_dut_interfaces(topo_vms, self.dut_interfaces)

                # This parameter is used for parallel
                if self.current_vm_name:
                    for k, v in topo_vms.items():
                        expected_vm_name = self.vm_names[self.vm_base_index + v['vm_offset']]
                        if expected_vm_name == self.current_vm_name:
                            self.VMs[k] = v
                            break
                else:
                    for k, v in topo_vms.items():
                        if self.vm_base_index + v['vm_offset'] < len(self.vm_names):
                            self.VMs[k] = v
        else:
            if 'DPUs' in self.topo and len(self.topo['DPUs']) > 0:
                self.vm_base = vm_base
                if vm_base in self.vm_names:
                    self.vm_base_index = self.vm_names.index(vm_base)
                else:
                    raise Exception('VM_base "%s" should be presented in current vm_names: %s' % (
                        vm_base, str(self.vm_names)))
                for k, v in self.topo['DPUs'].items():
                    if self.vm_base_index + v['vm_offset'] < len(self.vm_names):
                        self.VMs[k] = v

        if check_bridge:
            intf_names = os.listdir('/sys/class/net')
            for hostname, attrs in self.VMs.items():
                vmname = self.vm_names[self.vm_base_index + attrs['vm_offset']]
                vm_bridge_regx = OVS_FP_BRIDGE_REGEX % vmname
                num_intfs = len([intf for intf in intf_names if re.search(vm_bridge_regx, intf)])
                if len(attrs['vlans']) > num_intfs:
                    raise Exception("Wrong vlans parameter for hostname %s, vm %s. Too many vlans. Maximum is %d"
                                    % (hostname, vmname, num_intfs))

        self.VM_LINKs = {}
        if 'VM_LINKs' in self.topo:
            for k, v in self.topo['VM_LINKs'].items():
                self.VM_LINKs[k] = v

        self.OVS_LINKs = {}
        if 'OVS_LINKs' in self.topo:
            for k, v in self.topo['OVS_LINKs'].items():
                self.OVS_LINKs[k] = v

        self._is_multi_duts = True if len(self.duts_name) > 1 else False
        # For now distinguish a cable topology since it does not contain any vms and there are two ToR's
        self._is_cable = True if len(
            self.duts_name) > 1 and 'VMs' not in self.topo else False

        self.host_interfaces = self.topo.get('host_interfaces', [])
        if self.dut_interfaces:
            self.host_interfaces = MultiServersUtils.filter_by_dut_interfaces(
                self.host_interfaces,
                self.dut_interfaces
            )
        self.disabled_host_interfaces = self.topo.get(
            'disabled_host_interfaces', [])
        self.host_interfaces_active_active = self.topo.get(
            'host_interfaces_active_active', [])
        if self.host_interfaces_active_active:
            self.netns = NETNS_NAME_TEMPLATE % self.vm_set_name
            self.mux_cable_facts = generate_mux_cable_facts(self.topo)
        else:
            self.netns = None
            self.mux_cable_facts = {}

        self.devices_interconnect_interfaces = self.topo.get(
            'devices_interconnect_interfaces', {})

        self.duts_fp_ports = duts_fp_ports

        self.injected_fp_ports = self.extract_vm_vlans()
        self.injected_VM_ports = self.extract_vm_ovs()

        self.bp_bridge = ROOT_BACK_BR_TEMPLATE % self.vm_set_name

        if self._is_vs_chassis:
            self._vs_chassis_midplane_br_name = VS_CHASSIS_MIDPLANE_BRIDGE_NAME_TEMPLATE.format(vm_set_name=vm_set_name)
            self._vs_chassis_inband_br_name = VS_CHASSIS_INBAND_BRIDGE_NAME_TEMPLATE.format(vm_set_name=vm_set_name)
            if len(self._vs_chassis_midplane_br_name) > MAX_INTF_LEN:
                raise ValueError("The length of VS chassis midplane bridge name is too long.")
            if len(self._vs_chassis_inband_br_name) > MAX_INTF_LEN:
                raise ValueError("The length of VS chassis inband bridge name is too long.")

        # if the device is a bt0, build the mapping from interface to vlan id
        if self.dut_type == BACKEND_TOR_TYPE:
            default_vlan_config = self.topo.get("DUT", {}).get(
                "vlan_configs", {}).get("default_vlan_config")
            if not default_vlan_config:
                raise ValueError("Topology has no default vlan config.")
            if default_vlan_config not in self.topo["DUT"]["vlan_configs"]:
                raise ValueError(
                    "Topology has no definition for default vlan config %s" % default_vlan_config)
            vlan_config = self.topo["DUT"]["vlan_configs"][default_vlan_config]
            self.vlan_ids = {}
            for vlan in vlan_config.values():
                for intf in vlan["intfs"]:
                    self.vlan_ids[str(intf)] = str(vlan["id"])

    @property
    def dut_type(self):
        """Return the dut_type in vm configuration if present."""
        if not hasattr(self, "_dut_type"):
            for properties in self.vm_properties.values():
                dut_type = properties.get("dut_type")
                if dut_type:
                    self._dut_type = dut_type
                    break
            else:
                self._dut_type = None
        return self._dut_type

    def _parse_host_interfaces(self, host_interfaces):
        """
        Parse host interfaces.

        for single DUT, host interface like [0, 1, 2, ...],
        where the number is the port index starting from 0.

        For multi DUT, host interface like [(0, 1), (0, 2), (1, 1), (1, 2), ...],
        or [[(0, 1, 1), (1, 1, 1)], [(0, 2, 2), (1, 2, 2)]]
        where the tuple is (dut_index, dut_port_index) or (dut_index, dut_port_index, ptf_port_index), both starting
        from 0.

        For dual-tor, host interface look like [[(0, 1), (1, 1)], [(0, 2), (1,2)], ...],
        or [[(0, 1, 1), (1, 1, 1)], [(0, 2, 2), (1, 2, 2)]]
        where one interface consists of multiple ports to DUT.

        Example: [[(0, 2, 2), (1, 2, 2)], ] means that the PTF host interface 2 connects to port2@dut0 and port2@dut1

        Example: [[(0, 1), (1, 1)], ] means the PTF host interface connects to port1@dut0 and port1@dut1.
        """
        if self._is_multi_duts:
            _host_interfaces = []
            for intf in host_interfaces:
                intfs = intf.split(',')
                # re.split('\.|@', s) is to split string 's' by characters '.' or '@' and return a list.
                # The tuple may has 2 or 3 items:
                # (dut_index, dut_port_index) or (dut_index, dut_port_index, ptf_port_index)
                if len(intfs) > 1:
                    _host_interfaces.append(
                        [tuple(map(int, re.split(r'\.|@', x.strip()))) for x in intfs])
                else:
                    _host_interfaces.append(
                        tuple(map(int, re.split(r'\.|@', intfs[0].strip()))))
            return _host_interfaces
        else:
            return host_interfaces

    @property
    def host_interfaces(self):
        return self._host_interfaces

    @host_interfaces.setter
    def host_interfaces(self, value):
        self._host_interfaces = self._parse_host_interfaces(value)

    @property
    def disabled_host_interfaces(self):
        return self._disabled_host_interfaces

    @disabled_host_interfaces.setter
    def disabled_host_interfaces(self, value):
        self._disabled_host_interfaces = self._parse_host_interfaces(value)

    @property
    def host_interfaces_active_active(self):
        return self._host_interfaces_active_active

    @host_interfaces_active_active.setter
    def host_interfaces_active_active(self, value):
        self._host_interfaces_active_active = self._parse_host_interfaces(
            value)

    def extract_vm_vlans(self):
        vlans = {}
        for vm, attr in self.VMs.items():
            vlans[vm] = attr['vlans'][:]

        return vlans

    def extract_vm_ovs(self):
        vlans = {}
        for _, attr in self.OVS_LINKs.items():
            VM = self.vm_names[self.vm_base_index + attr['start_vm_offset']]
            vlans[VM] = attr['vlans'][:]
        return vlans

    def add_network_namespace(self):
        """Create a network namespace."""
        self.delete_network_namespace()
        VMTopology.cmd("ip netns add %s" % self.netns)

    def delete_network_namespace(self):
        """Delete a network namespace."""
        if os.path.exists("/var/run/netns/%s" % self.netns):
            VMTopology.cmd("ip netns delete %s" % self.netns)

    def enable_arp_filter_netns(self):
        """ENable ARP filter in the netns."""
        VMTopology.cmd("ip netns exec %s sysctl -w net.ipv4.conf.all.arp_filter=1" % self.netns)

    def add_mgmt_port_to_netns(self, mgmt_bridge, mgmt_ip, mgmt_gw, mgmt_ipv6_addr=None, mgmt_gw_v6=None):
        if VMTopology.intf_not_exists(MGMT_PORT_NAME, netns=self.netns):
            self.add_br_if_to_netns(
                mgmt_bridge, NETNS_MGMT_IF_TEMPLATE % self.vm_set_name, MGMT_PORT_NAME)
        self.add_ip_to_netns_if(MGMT_PORT_NAME, mgmt_ip, ipv6_addr=mgmt_ipv6_addr,
                                default_gw=mgmt_gw, default_gw_v6=mgmt_gw_v6)

    def create_bridges(self):
        for vm in self.vm_names:
            for fp_num in range(self.max_fp_num):
                fp_br_name = adaptive_name(OVS_FP_BRIDGE_TEMPLATE, vm, fp_num)
                self.create_ovs_bridge(fp_br_name, self.fp_mtu)

    def create_ovs_bridge(self, bridge_name, mtu):
        logging.info('=== Create bridge %s with mtu %d ===' %
                     (bridge_name, mtu))
        VMTopology.cmd('ovs-vsctl --may-exist add-br %s' % bridge_name)

        if mtu != DEFAULT_MTU:
            VMTopology.cmd('ifconfig %s mtu %d' % (bridge_name, mtu))

        VMTopology.cmd('ifconfig %s up' % bridge_name)

    def destroy_bridges(self):
        for vm in self.vm_names:
            for fp_num in range(self.max_fp_num):
                fp_br_name = adaptive_name(OVS_FP_BRIDGE_TEMPLATE, vm, fp_num)
                self.destroy_ovs_bridge(fp_br_name)

    def destroy_ovs_bridge(self, bridge_name):
        logging.info('=== Destroy bridge %s ===' % bridge_name)
        VMTopology.cmd('ovs-vsctl --if-exists del-br %s' % bridge_name)

    def add_injected_fp_ports_to_docker(self):
        """
        add injected front panel ports to docker


            PTF (int_if) ----------- injected port (ext_if)

        """
        for vm, vlans in self.injected_fp_ports.items():
            for vlan in vlans:
                (_, _, ptf_index) = VMTopology.parse_vm_vlan_port(vlan)
                ext_if = adaptive_name(
                    INJECTED_INTERFACES_TEMPLATE, self.vm_set_name, ptf_index)
                int_if = PTF_FP_IFACE_TEMPLATE % ptf_index
                properties = self.vm_properties.get(vm, {})
                create_vlan_subintf = properties.get('device_type') in (
                    BACKEND_TOR_TYPE, BACKEND_LEAF_TYPE)
                if create_vlan_subintf:
                    vlan_subintf_sep = properties.get(
                        'sub_interface_separator', SUB_INTERFACE_SEPARATOR)
                    vlan_subintf_vlan_id = properties.get(
                        'sub_interface_vlan_id', SUB_INTERFACE_VLAN_ID)
                    self.add_veth_if_to_docker(
                        ext_if, int_if,
                        create_vlan_subintf=create_vlan_subintf,
                        sub_interface_separator=vlan_subintf_sep,
                        sub_interface_vlan_id=vlan_subintf_vlan_id
                    )
                else:
                    self.add_veth_if_to_docker(ext_if, int_if)

    def add_injected_VM_ports_to_docker(self):
        for k, attr in self.OVS_LINKs.items():
            vlans = attr['vlans'][:]
            for vlan in vlans:
                (_, _, ptf_index) = VMTopology.parse_vm_vlan_port(vlan)
                int_if = PTF_FP_IFACE_TEMPLATE % ptf_index
                injected_iface = adaptive_name(INJECTED_INTERFACES_TEMPLATE, self.vm_set_name, ptf_index)
                self.add_veth_if_to_docker(injected_iface, int_if)

    def add_mgmt_port_to_docker(self, mgmt_bridge, mgmt_ip, mgmt_gw,
                                mgmt_ipv6_addr=None, mgmt_gw_v6=None, extra_mgmt_ip_addr=None,
                                api_server_pid=None):
        if api_server_pid:
            self.pid = api_server_pid
        if VMTopology.intf_not_exists(MGMT_PORT_NAME, pid=self.pid):
            if api_server_pid is None:
                self.add_br_if_to_docker(
                    mgmt_bridge, PTF_MGMT_IF_TEMPLATE % self.vm_set_name, MGMT_PORT_NAME)
            else:
                self.add_br_if_to_docker(
                    mgmt_bridge, 'apiserver', MGMT_PORT_NAME)
        self.add_ip_to_docker_if(MGMT_PORT_NAME, mgmt_ip, mgmt_ipv6_addr=mgmt_ipv6_addr,
                                 mgmt_gw=mgmt_gw, mgmt_gw_v6=mgmt_gw_v6,
                                 extra_mgmt_ip_addr=extra_mgmt_ip_addr, api_server_pid=api_server_pid)

    def add_bp_port_to_docker(self, mgmt_ip, mgmt_ipv6):
        self.add_br_if_to_docker(
            self.bp_bridge, PTF_BP_IF_TEMPLATE % self.vm_set_name, BP_PORT_NAME)
        self.add_ip_to_docker_if(BP_PORT_NAME, mgmt_ip, mgmt_ipv6)
        VMTopology.iface_disable_txoff(BP_PORT_NAME, self.pid)

    def add_br_if_to_docker(self, bridge, ext_if, int_if):
        # add unique suffix to int_if to support multiple tasks run concurrently
        tmp_int_if = int_if + \
            VMTopology._generate_fingerprint(ext_if, MAX_INTF_LEN - len(int_if))
        logging.info('=== For veth pair, add %s to bridge %s, set %s to PTF docker, tmp intf %s' % (
            ext_if, bridge, int_if, tmp_int_if))
        if VMTopology.intf_not_exists(ext_if):
            VMTopology.cmd("ip link add %s type veth peer name %s" %
                           (ext_if, tmp_int_if))

        _, if_to_br = VMTopology.brctl_show(bridge)
        if ext_if not in if_to_br:
            VMTopology.cmd("brctl addif %s %s" % (bridge, ext_if))

        VMTopology.iface_up(ext_if)

        if VMTopology.intf_exists(tmp_int_if) and VMTopology.intf_not_exists(tmp_int_if, pid=self.pid):
            VMTopology.cmd("ip link set dev %s netns %s " % (tmp_int_if, self.pid))
            VMTopology.cmd("nsenter -t %s -n ip link set dev %s name %s" % (self.pid, tmp_int_if, int_if))

        VMTopology.iface_up(int_if, pid=self.pid)

    def add_br_if_to_netns(self, bridge, ext_if, int_if):
        """Create a veth pair to connect the netns to the bridge."""
        # add unique suffix to int_if to support multiple tasks run concurrently
        tmp_int_if = int_if + \
            VMTopology._generate_fingerprint(ext_if, MAX_INTF_LEN - len(int_if))
        logging.info('=== For veth pair, add %s to bridge %s, set %s to netns, tmp intf %s' % (
            ext_if, bridge, int_if, tmp_int_if))
        if VMTopology.intf_not_exists(ext_if):
            VMTopology.cmd("ip link add %s type veth peer name %s" %
                           (ext_if, tmp_int_if))

        _, if_to_br = VMTopology.brctl_show(bridge)
        if ext_if not in if_to_br:
            VMTopology.cmd("brctl addif %s %s" % (bridge, ext_if))

        VMTopology.iface_up(ext_if)

        if VMTopology.intf_exists(tmp_int_if) and VMTopology.intf_not_exists(tmp_int_if, netns=self.netns):
            VMTopology.cmd("ip link set dev %s netns %s" % (tmp_int_if, self.netns))
            VMTopology.cmd("ip netns exec %s ip link set dev %s name %s" % (self.netns, tmp_int_if, int_if))

        VMTopology.iface_up(int_if, netns=self.netns)

    def add_ip_to_docker_if(self, int_if, mgmt_ip_addr, mgmt_ipv6_addr=None,
                            mgmt_gw=None, mgmt_gw_v6=None, extra_mgmt_ip_addr=None,
                            api_server_pid=None):
        if api_server_pid:
            self.pid = api_server_pid

        if VMTopology.intf_exists(int_if, pid=self.pid):
            if not VMTopology.ip_exists(int_if, mgmt_ip_addr, pid=self.pid):
                VMTopology.cmd("nsenter -t %s -n ip addr add %s dev %s" %
                               (self.pid, mgmt_ip_addr, int_if))
            if extra_mgmt_ip_addr is not None:
                for ip_addr in extra_mgmt_ip_addr:
                    if ip_addr != "":
                        VMTopology.cmd("nsenter -t %s -n ip addr add %s dev %s" %
                                       (self.pid, ip_addr, int_if))
            if mgmt_gw:
                if api_server_pid:
                    VMTopology.cmd(
                        "nsenter -t %s -n ip route del default" % (self.pid))
                if not VMTopology.route_exists(mgmt_gw, pid=self.pid):
                    VMTopology.cmd(
                        "nsenter -t %s -n ip route add default via %s dev %s" % (self.pid, mgmt_gw, int_if))
            if mgmt_ipv6_addr:
                if not VMTopology.ip_exists(int_if, mgmt_ipv6_addr, pid=self.pid, ipv6=True):
                    VMTopology.cmd("nsenter -t %s -n ip -6 addr add %s dev %s" %
                                   (self.pid, mgmt_ipv6_addr, int_if))
            if mgmt_ipv6_addr and mgmt_gw_v6:
                if not VMTopology.route_exists(mgmt_gw_v6, pid=self.pid, ipv6=True):
                    VMTopology.cmd(
                        "nsenter -t %s -n ip -6 route add default via %s dev %s" % (self.pid, mgmt_gw_v6, int_if))

    def add_ip_to_netns_if(self, int_if, ip_addr, ipv6_addr=None, default_gw=None, default_gw_v6=None):
        """Add ip address to netns interface."""
        if VMTopology.intf_exists(int_if, netns=self.netns):
            VMTopology.cmd("ip netns exec %s ip addr flush dev %s" %
                           (self.netns, int_if))
            VMTopology.cmd("ip netns exec %s ip addr add %s dev %s" %
                           (self.netns, ip_addr, int_if))
            if default_gw:
                VMTopology.cmd(
                    "ip netns exec %s ip route flush default" % (self.netns))
                VMTopology.cmd("ip netns exec %s ip route add default via %s dev %s" % (
                    self.netns, default_gw, int_if))
            if ipv6_addr:
                VMTopology.cmd(
                    "ip netns exec %s ip -6 addr flush dev %s" % (self.netns, int_if))
                VMTopology.cmd("ip netns exec %s ip -6 addr add %s dev %s" %
                               (self.netns, ipv6_addr, int_if))
                if default_gw_v6:
                    VMTopology.cmd(
                        "ip netns exec %s ip -6 route flush default" % (self.netns))
                    VMTopology.cmd("ip netns exec %s ip -6 route add default via %s dev %s" %
                                   (self.netns, default_gw_v6, int_if))

    def add_dut_if_to_docker(self, iface_name, dut_iface):
        logging.info("=== Add DUT interface %s to PTF docker as %s ===" %
                     (dut_iface, iface_name))
        if VMTopology.intf_exists(dut_iface) \
                and VMTopology.intf_not_exists(dut_iface, pid=self.pid) \
                and VMTopology.intf_not_exists(iface_name, pid=self.pid):
            VMTopology.cmd("ip link set dev %s netns %s" % (dut_iface, self.pid))

        if VMTopology.intf_exists(dut_iface, pid=self.pid) and VMTopology.intf_not_exists(iface_name, pid=self.pid):
            VMTopology.cmd("nsenter -t %s -n ip link set dev %s name %s" % (self.pid, dut_iface, iface_name))

        VMTopology.iface_up(iface_name, pid=self.pid)

    def add_dut_vlan_subif_to_docker(self, iface_name, vlan_separator, vlan_id):
        """Create a vlan sub interface for the ptf interface."""
        if VMTopology.intf_not_exists(iface_name, pid=self.pid):
            raise ValueError("Interface %s not present in docker" % iface_name)
        vlan_sub_iface_name = iface_name + vlan_separator + vlan_id
        VMTopology.cmd("nsenter -t %s -n ip link add link %s name %s type vlan id %s" %
                       (self.pid, iface_name, vlan_sub_iface_name, vlan_id))
        VMTopology.cmd("nsenter -t %s -n ip link set %s up" %
                       (self.pid, vlan_sub_iface_name))

    def remove_dut_if_from_docker(self, iface_name, dut_iface):
        logging.info("=== Restore docker interface %s as dut interface %s ===" % (iface_name, dut_iface))
        if self.pid is None:
            return

        if VMTopology.intf_exists(iface_name, pid=self.pid):
            VMTopology.iface_down(iface_name, pid=self.pid)

            if VMTopology.intf_not_exists(dut_iface, pid=self.pid):
                VMTopology.cmd("nsenter -t %s -n ip link set dev %s name %s" %
                               (self.pid, iface_name, dut_iface))

        if VMTopology.intf_not_exists(dut_iface) and VMTopology.intf_exists(dut_iface, pid=self.pid):
            VMTopology.cmd(
                "nsenter -t %s -n ip link set dev %s netns 1" % (self.pid, dut_iface))

    def remove_dut_vlan_subif_from_docker(self, iface_name, vlan_separator, vlan_id):
        """Remove the vlan sub interface created for the ptf interface."""
        if self.pid is None:
            return

        vlan_sub_iface_name = iface_name + vlan_separator + vlan_id
        if VMTopology.intf_exists(vlan_sub_iface_name, pid=self.pid):
            VMTopology.iface_down(vlan_sub_iface_name, pid=self.pid)
            VMTopology.cmd("nsenter -t %s -n ip link del %s" %
                           (self.pid, vlan_sub_iface_name))

    def add_veth_if_to_docker(self, ext_if, int_if, create_vlan_subintf=False, **kwargs):
        """Create vethernet devices (ext_if, int_if) and put int_if into the ptf docker."""
        logging.info('=== Create veth pair %s/%s, set %s to PTF docker namespace ===' %
                     (ext_if, int_if, int_if))
        if create_vlan_subintf:
            try:
                vlan_subintf_sep = kwargs["sub_interface_separator"]
                vlan_subintf_vlan_id = kwargs["sub_interface_vlan_id"]
            except KeyError:
                raise TypeError(
                    "Missing arguments for function 'add_veth_if_to_docker'")

        reserved_space = len(
            vlan_subintf_sep + vlan_subintf_vlan_id) if create_vlan_subintf else 0
        t_int_if = adaptive_temporary_interface(
            self.vm_set_name, int_if, reserved_space=reserved_space)
        if create_vlan_subintf:
            int_sub_if = int_if + vlan_subintf_sep + vlan_subintf_vlan_id
            t_int_sub_if = t_int_if + vlan_subintf_sep + vlan_subintf_vlan_id

        if VMTopology.intf_exists(t_int_if):
            VMTopology.cmd("ip link del dev %s" % t_int_if)

        if VMTopology.intf_not_exists(ext_if):
            VMTopology.cmd("ip link add %s type veth peer name %s" %
                           (ext_if, t_int_if))
            if create_vlan_subintf:
                VMTopology.cmd("vconfig add %s %s" %
                               (t_int_if, vlan_subintf_vlan_id))

        if self.fp_mtu != DEFAULT_MTU:
            VMTopology.cmd("ip link set dev %s mtu %d" % (ext_if, self.fp_mtu))
            if VMTopology.intf_exists(t_int_if):
                VMTopology.cmd("ip link set dev %s mtu %d" %
                               (t_int_if, self.fp_mtu))
            elif VMTopology.intf_exists(t_int_if, pid=self.pid):
                VMTopology.cmd("nsenter -t %s -n ip link set dev %s mtu %d" %
                               (self.pid, t_int_if, self.fp_mtu))
            elif VMTopology.intf_exists(int_if, pid=self.pid):
                VMTopology.cmd("nsenter -t %s -n ip link set dev %s mtu %d" %
                               (self.pid, int_if, self.fp_mtu))
            if create_vlan_subintf:
                if VMTopology.intf_exists(t_int_sub_if):
                    VMTopology.cmd("ip link set dev %s mtu %d" %
                                   (t_int_sub_if, self.fp_mtu))
                elif VMTopology.intf_exists(t_int_sub_if, pid=self.pid):
                    VMTopology.cmd("nsenter -t %s -n ip link set dev %s mtu %d" %
                                   (self.pid, t_int_sub_if, self.fp_mtu))
                elif VMTopology.intf_exists(int_sub_if, pid=self.pid):
                    VMTopology.cmd("nsenter -t %s -n ip link set dev %s mtu %d" %
                                   (self.pid, int_sub_if, self.fp_mtu))

        VMTopology.iface_up(ext_if)

        if VMTopology.intf_exists(t_int_if) \
                and VMTopology.intf_not_exists(t_int_if, pid=self.pid) \
                and VMTopology.intf_not_exists(int_if, pid=self.pid):
            VMTopology.cmd("ip link set dev %s netns %s" %
                           (t_int_if, self.pid))
        if create_vlan_subintf \
                and VMTopology.intf_exists(t_int_sub_if) \
                and VMTopology.intf_not_exists(t_int_sub_if, pid=self.pid) \
                and VMTopology.intf_not_exists(int_sub_if, pid=self.pid):
            VMTopology.cmd("ip link set dev %s netns %s" %
                           (t_int_sub_if, self.pid))

        if VMTopology.intf_exists(t_int_if, pid=self.pid) and VMTopology.intf_not_exists(int_if, pid=self.pid):
            VMTopology.cmd("nsenter -t %s -n ip link set dev %s name %s" %
                           (self.pid, t_int_if, int_if))
        if create_vlan_subintf \
                and VMTopology.intf_exists(t_int_sub_if, pid=self.pid) \
                and VMTopology.intf_not_exists(int_sub_if, pid=self.pid):
            VMTopology.cmd("nsenter -t %s -n ip link set dev %s name %s" %
                           (self.pid, t_int_sub_if, int_sub_if))

        VMTopology.iface_up(int_if, pid=self.pid)
        if create_vlan_subintf:
            VMTopology.iface_up(int_sub_if, pid=self.pid)

    def add_veth_if_to_netns(self, ext_if, int_if):
        """Create vethernet devices (ext_if, int_if) and put int_if into the netns for active-active."""
        logging.info('=== Create veth pair %s/%s, set %s to netns %s ===' %
                     (ext_if, int_if, int_if, self.netns))

        t_int_if = adaptive_temporary_interface(self.vm_set_name, int_if)

        if VMTopology.intf_exists(t_int_if):
            VMTopology.cmd("ip link del dev %s" % t_int_if)

        if VMTopology.intf_not_exists(ext_if):
            VMTopology.cmd("ip link add %s type veth peer name %s" %
                           (ext_if, t_int_if))

        if self.fp_mtu != DEFAULT_MTU:
            VMTopology.cmd("ip link set dev %s mtu %d" % (ext_if, self.fp_mtu))
            if VMTopology.intf_exists(t_int_if):
                VMTopology.cmd("ip link set dev %s mtu %d" %
                               (t_int_if, self.fp_mtu))
            elif VMTopology.intf_exists(t_int_if, netns=self.netns):
                VMTopology.cmd("ip netns exec %s ip link set dev %s mtu %d" % (
                    self.netns, t_int_if, self.fp_mtu))
            elif VMTopology.intf_exists(int_if, netns=self.netns):
                VMTopology.cmd("ip netns exec %s ip link set dev %s mtu %d" % (
                    self.netns, int_if, self.fp_mtu))

        VMTopology.iface_up(ext_if)

        if VMTopology.intf_exists(t_int_if) \
                and VMTopology.intf_not_exists(t_int_if, netns=self.netns) \
                and VMTopology.intf_not_exists(int_if, netns=self.netns):
            VMTopology.cmd("ip link set dev %s netns %s" %
                           (t_int_if, self.netns))

        if VMTopology.intf_exists(t_int_if, netns=self.netns) and VMTopology.intf_not_exists(int_if, netns=self.netns):
            VMTopology.cmd("ip netns exec %s ip link set dev %s name %s" % (
                self.netns, t_int_if, int_if))

        VMTopology.iface_up(int_if, netns=self.netns)

    def bind_mgmt_port(self, br_name, mgmt_port):
        logging.info('=== Bind mgmt port %s to bridge %s ===' %
                     (mgmt_port, br_name))
        _, if_to_br = VMTopology.brctl_show(br_name)
        if mgmt_port not in if_to_br:
            VMTopology.cmd("brctl addif %s %s" % (br_name, mgmt_port))

    def unbind_mgmt_port(self, mgmt_port):
        _, if_to_br = VMTopology.brctl_show()
        if mgmt_port in if_to_br:
            VMTopology.cmd("brctl delif %s %s" %
                           (if_to_br[mgmt_port], mgmt_port))

    def bind_devices_interconnect(self):
        for link_index, vlans in self.devices_interconnect_interfaces.items():
            interconnection_bridge = OVS_INTERCONNECTION_BRIDGE_TEMPLATE % (
                self.vm_set_name, link_index)
            self.create_ovs_bridge(interconnection_bridge, self.fp_mtu)
            (dut_index, vlan_index,
             ptf_index) = VMTopology.parse_vm_vlan_port(vlans[0])
            (dut_index_1, vlan_index_1,
             ptf_index_1) = VMTopology.parse_vm_vlan_port(vlans[-1])
            vlan1_iface = self.duts_fp_ports[self.duts_name[dut_index]][str(
                vlan_index)]
            vlan2_iface = self.duts_fp_ports[self.duts_name[dut_index_1]][str(
                vlan_index_1)]
            self.bind_devices_interconnect_ports(
                interconnection_bridge, vlan1_iface, vlan2_iface)

    def unbind_devices_interconnect(self):
        for link_index, vlans in self.devices_interconnect_interfaces.items():
            interconnection_bridge = OVS_INTERCONNECTION_BRIDGE_TEMPLATE % (
                self.vm_set_name, link_index)
            (dut_index, vlan_index,
             ptf_index) = VMTopology.parse_vm_vlan_port(vlans[0])
            (dut_index_1, vlan_index_1,
             ptf_index_1) = VMTopology.parse_vm_vlan_port(vlans[-1])
            vlan1_iface = self.duts_fp_ports[self.duts_name[dut_index]][str(
                vlan_index)]
            vlan2_iface = self.duts_fp_ports[self.duts_name[dut_index_1]][str(
                vlan_index_1)]
            self.unbind_ovs_port(interconnection_bridge, vlan1_iface)
            self.unbind_ovs_port(interconnection_bridge, vlan2_iface)
            self.destroy_ovs_bridge(interconnection_bridge)

    def bind_devices_interconnect_ports(self, br_name, vlan1_iface, vlan2_iface):
        ports = VMTopology.get_ovs_br_ports(br_name)
        if vlan1_iface not in ports:
            VMTopology.cmd('ovs-vsctl --may-exist add-port %s %s' % (br_name, vlan1_iface))
        if vlan2_iface not in ports:
            VMTopology.cmd('ovs-vsctl --may-exist add-port %s %s' % (br_name, vlan2_iface))
        bindings = VMTopology.get_ovs_port_bindings(br_name)
        vlan1_iface_id = bindings[vlan1_iface]
        vlan2_iface_id = bindings[vlan2_iface]
        # clear old bindings
        VMTopology.cmd('ovs-ofctl del-flows %s' % br_name)
        VMTopology.cmd("ovs-ofctl add-flow %s table=0,in_port=%s,action=output:%s" %
                       (br_name, vlan1_iface_id, vlan2_iface_id))
        VMTopology.cmd("ovs-ofctl add-flow %s table=0,in_port=%s,action=output:%s" %
                       (br_name, vlan2_iface_id, vlan1_iface_id))

    def bind_fp_ports(self, disconnect_vm=False, batch_mode=False):
        """
        bind dut front panel ports to VMs

                            +----------------------+
                            |     OVS_FP_BRIDGE    |
                 +----+     |                      |
                 | VM +-----+ vm_iface             |      +-----+
                 +----+     |        duts_fp_port  +------+ DUT |
                            |                      |      +-----+
                 +-----+    |                      |
                 | PTF +----+ injected_iface       |
                 +-----+    |                      |
                            +----------------------+

        """
        bind_ovs_ports_args = []
        for attr in self.VMs.values():
            for idx, vlan in enumerate(attr['vlans']):
                br_name = adaptive_name(
                    OVS_FP_BRIDGE_TEMPLATE, self.vm_names[self.vm_base_index + attr['vm_offset']], idx)
                vm_iface = OVS_FP_TAP_TEMPLATE % (
                    self.vm_names[self.vm_base_index + attr['vm_offset']], idx)
                (dut_index, vlan_index, ptf_index) = VMTopology.parse_vm_vlan_port(vlan)
                injected_iface = adaptive_name(
                    INJECTED_INTERFACES_TEMPLATE, self.vm_set_name, ptf_index)
                if len(self.duts_fp_ports[self.duts_name[dut_index]]) == 0:
                    continue
                bind_ovs_ports_args.append(
                    (br_name, self.duts_fp_ports[self.duts_name[dut_index]][str(vlan_index)],
                     injected_iface, vm_iface, disconnect_vm)
                )
        if batch_mode:
            with VMTopologyWorker.safe_subprocess_manager() as [processes, tmpdir]:
                self.worker.map(lambda args: self.bind_ovs_ports(*args, processes=processes,
                                                                 tmpdir=tmpdir), bind_ovs_ports_args)
        else:
            self.worker.map(lambda args: self.bind_ovs_ports(*args), bind_ovs_ports_args)

        for k, attr in self.VM_LINKs.items():
            logging.info("Create VM links for {} : {}".format(k, attr))
            br_name = "br_{}".format(k.lower())
            port1 = OVS_FP_TAP_TEMPLATE % (
                self.vm_names[self.vm_base_index + attr['start_vm_offset']],
                attr['start_vm_port_idx']
            )
            port2 = OVS_FP_TAP_TEMPLATE % (
                self.vm_names[self.vm_base_index + attr['end_vm_offset']],
                attr['end_vm_port_idx']
            )

            self.bind_vm_link(br_name, port1, port2)

        for k, attr in self.OVS_LINKs.items():
            logging.info("Create OVS links for {} : {}".format(k, attr))
            br_name = "br_{}".format(k.lower())
            port1 = OVS_FP_TAP_TEMPLATE % (
                self.vm_names[self.vm_base_index + attr['start_vm_offset']],
                attr['start_vm_port_idx']
            )
            port2 = OVS_FP_TAP_TEMPLATE % (
                self.vm_names[self.vm_base_index + attr['end_vm_offset']],
                attr['end_vm_port_idx']
            )
            self.create_ovs_bridge(br_name, 9000)
            vlans = attr['vlans']
            for vlan in vlans:
                (_, _, ptf_index) = VMTopology.parse_vm_vlan_port(vlan)
                injected_iface = adaptive_name(INJECTED_INTERFACES_TEMPLATE, self.vm_set_name, ptf_index)
                self.bind_ovs_ports(br_name, port1, injected_iface, port2, disconnect_vm)

    def unbind_fp_ports(self, batch_mode=False):
        logging.info("=== unbind front panel ports ===")
        unbind_ovs_ports_args = []
        for attr in self.VMs.values():
            for vlan_num, vlan in enumerate(attr['vlans']):
                br_name = adaptive_name(
                    OVS_FP_BRIDGE_TEMPLATE, self.vm_names[self.vm_base_index + attr['vm_offset']], vlan_num)
                vm_iface = OVS_FP_TAP_TEMPLATE % (
                    self.vm_names[self.vm_base_index + attr['vm_offset']], vlan_num)
                unbind_ovs_ports_args.append((br_name, vm_iface))

        if batch_mode:
            with VMTopologyWorker.safe_subprocess_manager() as [processes, _]:
                self.worker.map(lambda args: self.unbind_ovs_ports(*args, processes=processes), unbind_ovs_ports_args)
        else:
            self.worker.map(lambda args: self.unbind_ovs_ports(*args), unbind_ovs_ports_args)

        for k, attr in self.VM_LINKs.items():
            logging.info("Remove VM links for {} : {}".format(k, attr))
            br_name = "br_{}".format(k.lower())
            port1 = OVS_FP_TAP_TEMPLATE % (
                self.vm_names[self.vm_base_index + attr['start_vm_offset']],
                attr['start_vm_port_idx']
            )
            port2 = OVS_FP_TAP_TEMPLATE % (
                self.vm_names[self.vm_base_index + attr['end_vm_offset']],
                attr['end_vm_port_idx']
            )
            if "use_ovs" in attr and attr["use_ovs"] == 1:
                self.unbind_ovs_port(br_name, port1)
                self.unbind_ovs_port(br_name, port2)
                self.destroy_ovs_bridge(br_name)
            else:
                self.unbind_vm_link(br_name, port1, port2)

        for k, attr in self.OVS_LINKs.items():
            logging.info("Remove OVS links for {} : {}".format(k, attr))
            br_name = "br_{}".format(k.lower())
            port1 = OVS_FP_TAP_TEMPLATE % (
                self.vm_names[self.vm_base_index + attr['start_vm_offset']],
                attr['start_vm_port_idx']
            )
            port2 = OVS_FP_TAP_TEMPLATE % (
                self.vm_names[self.vm_base_index + attr['end_vm_offset']],
                attr['end_vm_port_idx']
            )
            self.create_ovs_bridge(br_name, 9000)
            vlans = attr['vlans']
            for vlan in vlans:
                (_, _, ptf_index) = VMTopology.parse_vm_vlan_port(vlan)
                injected_iface = adaptive_name(INJECTED_INTERFACES_TEMPLATE, self.vm_set_name, ptf_index)
                self.unbind_ovs_ports(br_name, port1)
                self.unbind_ovs_ports(br_name, port2)
                self.unbind_ovs_ports(br_name, injected_iface)

    def unbind_vm_link(self, br_name, port1, port2):
        _, if_to_br = VMTopology.brctl_show()
        if port1 in if_to_br:
            VMTopology.cmd("brctl delif %s %s" % (br_name, port1))
        if port2 in if_to_br:
            VMTopology.cmd("brctl delif %s %s" % (br_name, port2))
        VMTopology.cmd('brctl delbr %s || true' % br_name, shell=True, split_cmd=False)

    def bind_vm_link(self, br_name, port1, port2):
        if VMTopology.intf_not_exists(br_name):
            VMTopology.cmd('brctl addbr %s' % br_name)
        VMTopology.iface_up(br_name)

        # Remove port from ovs bridge
        br = VMTopology.get_ovs_bridge_by_port(port1)
        if br is not None:
            VMTopology.cmd('ovs-vsctl --if-exists del-port %s %s' % (br, port1))

        br = VMTopology.get_ovs_bridge_by_port(port2)
        if br is not None:
            VMTopology.cmd('ovs-vsctl --if-exists del-port %s %s' % (br, port2))

        m_to_ifs, _ = VMTopology.brctl_show()
        if port1 not in m_to_ifs[br_name]:
            VMTopology.cmd("brctl addif %s %s" % (br_name, port1))
        if port2 not in m_to_ifs[br_name]:
            VMTopology.cmd("brctl addif %s %s" % (br_name, port2))
        VMTopology.iface_up(port1)
        VMTopology.iface_up(port2)

    def bind_vm_backplane(self):

        if VMTopology.intf_not_exists(self.bp_bridge):
            VMTopology.cmd('brctl addbr %s' % self.bp_bridge)

        VMTopology.iface_up(self.bp_bridge)

        for attr in self.VMs.values():
            vm_name = self.vm_names[self.vm_base_index + attr['vm_offset']]
            bp_port_name = OVS_BP_TAP_TEMPLATE % vm_name

            br_to_ifs, _ = VMTopology.brctl_show()
            if bp_port_name not in br_to_ifs[self.bp_bridge]:
                VMTopology.cmd("brctl addif %s %s" %
                               (self.bp_bridge, bp_port_name))

            VMTopology.iface_up(bp_port_name)

    def unbind_vm_backplane(self):

        if VMTopology.intf_exists(self.bp_bridge):
            VMTopology.iface_down(self.bp_bridge)
            VMTopology.cmd('brctl delbr %s || true' % self.bp_bridge, shell=True, split_cmd=False)

    def bind_vs_chassis_ports(self, duts_midplane_ports, duts_inband_ports):
        # We have a KVM based virtaul chassis, create two ovs bridges, bind the midplane and inband ports
        self.create_ovs_bridge(self._vs_chassis_inband_br_name, self.fp_mtu)
        self.create_ovs_bridge(self._vs_chassis_midplane_br_name, self.fp_mtu)

        for dut in duts_midplane_ports.keys():
            self.bind_vs_dut_ports(
                self._vs_chassis_midplane_br_name, dut, duts_midplane_ports[dut])

        for dut in duts_inband_ports.keys():
            self.bind_vs_dut_ports(
                self._vs_chassis_inband_br_name, dut, duts_inband_ports[dut])

    def unbind_vs_chassis_ports(self, duts_midplane_ports, duts_inband_ports):
        # We have a KVM based virtaul chassis, bind the midplane and inband ports
        for dut in duts_midplane_ports.keys():
            self.unbind_vs_dut_ports(
                self._vs_chassis_midplane_br_name, dut, duts_midplane_ports[dut])

        for dut in duts_inband_ports.keys():
            self.unbind_vs_dut_ports(
                self._vs_chassis_inband_br_name, dut, duts_inband_ports[dut])

        self.destroy_ovs_bridge(self._vs_chassis_inband_br_name)
        self.destroy_ovs_bridge(self._vs_chassis_midplane_br_name)

    def bind_vs_dut_ports(self, br_name, dut_name, dut_ports):
        br_ports = VMTopology.get_ovs_br_ports(br_name)
        for port in dut_ports:
            br = VMTopology.get_ovs_bridge_by_port(port)
            if br is not None and br != br_name:
                VMTopology.cmd('ovs-vsctl --if-exists del-port {} {}'.format(br, port))

            if port not in br_ports:
                VMTopology.cmd('ovs-vsctl --may-exist add-port {} {}'.format(br_name, port))

    def unbind_vs_dut_ports(self, br_name, dut_name, dut_ports):
        """unbind all ports except the vm port from an ovs bridge"""
        if VMTopology.intf_exists(br_name):
            br_ports = VMTopology.get_ovs_br_ports(br_name)
            for port in dut_ports:
                if port in br_ports:
                    VMTopology.cmd('ovs-vsctl --if-exists del-port {} {}'.format(br_name, port))

    def bind_ovs_ports(self, br_name, dut_iface, injected_iface, vm_iface, disconnect_vm=False, **kwargs):
        """
        bind dut/injected/vm ports under an ovs bridge as follows

                                   +----------------------+
                                   |                      +---- dut_iface
            PTF (injected_iface) --+ OVS bridge (br_name) |
                                   |                      +---- vm_iface
                                   +----------------------+
        """
        br = VMTopology.get_ovs_bridge_by_port(injected_iface)
        if br is not None and br != br_name:
            VMTopology.cmd('ovs-vsctl --if-exists del-port %s %s' % (br, injected_iface))

        br = VMTopology.get_ovs_bridge_by_port(dut_iface)
        if br is not None and br != br_name:
            VMTopology.cmd('ovs-vsctl --if-exists del-port %s %s' % (br, dut_iface))

        br = VMTopology.get_ovs_bridge_by_port(vm_iface)
        if br is not None and br != br_name:
            VMTopology.cmd('ovs-vsctl --if-exists del-port %s %s' % (br, vm_iface))

        ports = VMTopology.get_ovs_br_ports(br_name)
        if injected_iface not in ports:
            VMTopology.cmd('ovs-vsctl --may-exist add-port %s %s' %
                           (br_name, injected_iface))

        if dut_iface not in ports:
            VMTopology.cmd('ovs-vsctl --may-exist add-port %s %s' % (br_name, dut_iface))

        if vm_iface not in ports:
            VMTopology.cmd('ovs-vsctl --may-exist add-port %s %s' % (br_name, vm_iface))

        bindings = VMTopology.get_ovs_port_bindings(br_name, [dut_iface])
        dut_iface_id = bindings[dut_iface]
        injected_iface_id = bindings[injected_iface]
        vm_iface_id = bindings[vm_iface]

        # clear old bindings
        VMTopology.cmd('ovs-ofctl del-flows %s' % br_name)

        if disconnect_vm:
            # Drop packets from VM
            VMTopology.cmd(
                "ovs-ofctl add-flow %s table=0,in_port=%s,action=drop" % (br_name, vm_iface_id))
        else:
            # Add flow from a VM to an external iface
            VMTopology.cmd("ovs-ofctl add-flow %s table=0,in_port=%s,action=output:%s" %
                           (br_name, vm_iface_id, dut_iface_id))

        if disconnect_vm:
            # Add flow from external iface to ptf container
            VMTopology.cmd("ovs-ofctl add-flow %s table=0,in_port=%s,action=output:%s" %
                           (br_name, dut_iface_id, injected_iface_id))
        else:
            bind_helper = VMTopology.cmd
            is_batch_mode = "processes" in kwargs
            all_cmds = []

            if is_batch_mode:
                bind_helper = lambda cmd: \
                    all_cmds.append(cmd.split()[-1])  # noqa: E731

            # Add flow from external iface to a VM and a ptf container
            # Allow BGP, IPinIP, fragmented packets, ICMP, SNMP packets and layer2 packets from DUT to neighbors
            # Block other traffic from DUT to EOS for EOS's stability,
            # Allow all traffic from DUT to PTF.
            bind_helper("ovs-ofctl add-flow %s table=0,priority=10,tcp,in_port=%s,tp_src=179,action=output:%s,%s" %
                        (br_name, dut_iface_id, vm_iface_id, injected_iface_id))
            bind_helper("ovs-ofctl add-flow %s table=0,priority=10,tcp,in_port=%s,tp_dst=179,action=output:%s,%s" %
                        (br_name, dut_iface_id, vm_iface_id, injected_iface_id))
            bind_helper("ovs-ofctl add-flow %s table=0,priority=10,tcp,in_port=%s,tp_dst=22,action=output:%s,%s" %
                        (br_name, dut_iface_id, vm_iface_id, injected_iface_id))
            bind_helper("ovs-ofctl add-flow %s table=0,priority=10,tcp,in_port=%s,tp_src=22,action=output:%s,%s" %
                        (br_name, dut_iface_id, vm_iface_id, injected_iface_id))
            bind_helper("ovs-ofctl add-flow %s table=0,priority=10,tcp6,in_port=%s,tp_src=179,action=output:%s,%s" %
                        (br_name, dut_iface_id, vm_iface_id, injected_iface_id))
            bind_helper("ovs-ofctl add-flow %s table=0,priority=10,tcp6,in_port=%s,tp_dst=179,action=output:%s,%s" %
                        (br_name, dut_iface_id, vm_iface_id, injected_iface_id))
            bind_helper("ovs-ofctl add-flow %s table=0,priority=10,tcp6,in_port=%s,tp_dst=22,action=output:%s,%s" %
                        (br_name, dut_iface_id, vm_iface_id, injected_iface_id))
            bind_helper("ovs-ofctl add-flow %s table=0,priority=10,tcp6,in_port=%s,tp_src=22,action=output:%s,%s" %
                        (br_name, dut_iface_id, vm_iface_id, injected_iface_id))
            bind_helper("ovs-ofctl add-flow %s table=0,priority=10,ip,in_port=%s,nw_proto=4,action=output:%s,%s" %
                        (br_name, dut_iface_id, vm_iface_id, injected_iface_id))
            bind_helper("ovs-ofctl add-flow %s table=0,priority=8,ip,in_port=%s,nw_frag=yes,action=output:%s,%s" %
                        (br_name, dut_iface_id, vm_iface_id, injected_iface_id))
            bind_helper("ovs-ofctl add-flow %s table=0,priority=8,ipv6,in_port=%s,nw_frag=yes,action=output:%s,%s" %
                        (br_name, dut_iface_id, vm_iface_id, injected_iface_id))
            bind_helper("ovs-ofctl add-flow %s table=0,priority=8,icmp,in_port=%s,action=output:%s,%s" %
                        (br_name, dut_iface_id, vm_iface_id, injected_iface_id))
            bind_helper("ovs-ofctl add-flow %s table=0,priority=8,icmp6,in_port=%s,action=output:%s,%s" %
                        (br_name, dut_iface_id, vm_iface_id, injected_iface_id))
            bind_helper("ovs-ofctl add-flow %s table=0,priority=8,udp,in_port=%s,udp_src=161,action=output:%s,%s" %
                        (br_name, dut_iface_id, vm_iface_id, injected_iface_id))
            bind_helper("ovs-ofctl add-flow %s table=0,priority=8,udp,in_port=%s,udp_src=53,action=output:%s" %
                        (br_name, dut_iface_id, vm_iface_id))
            bind_helper("ovs-ofctl add-flow %s table=0,priority=8,udp6,in_port=%s,udp_src=161,action=output:%s,%s" %
                        (br_name, dut_iface_id, vm_iface_id, injected_iface_id))
            bind_helper("ovs-ofctl add-flow %s table=0,priority=6,udp6,in_port=%s,udp_dst=4784,action=output:%s" %
                        (br_name, dut_iface_id, injected_iface_id))
            bind_helper("ovs-ofctl add-flow %s table=0,priority=5,ip,in_port=%s,action=output:%s" %
                        (br_name, dut_iface_id, injected_iface_id))
            bind_helper("ovs-ofctl add-flow %s table=0,priority=5,ipv6,in_port=%s,action=output:%s,%s" %
                        (br_name, dut_iface_id, vm_iface_id, injected_iface_id))
            bind_helper("ovs-ofctl add-flow %s table=0,priority=3,in_port=%s,action=output:%s,%s" %
                        (br_name, dut_iface_id, vm_iface_id, injected_iface_id))
            bind_helper("ovs-ofctl add-flow %s table=0,priority=10,ip,in_port=%s,nw_proto=89,action=output:%s,%s" %
                        (br_name, dut_iface_id, vm_iface_id, injected_iface_id))
            bind_helper("ovs-ofctl add-flow %s table=0,priority=10,ipv6,in_port=%s,nw_proto=89,action=output:%s,%s" %
                        (br_name, dut_iface_id, vm_iface_id, injected_iface_id))

        # Add flow for BFD Control packets (UDP port 3784)
            bind_helper("ovs-ofctl add-flow %s 'table=0,priority=10,udp,in_port=%s,\
                        udp_dst=3784,action=output:%s,%s'" %
                        (br_name, dut_iface_id, vm_iface_id, injected_iface_id))
            bind_helper("ovs-ofctl add-flow %s 'table=0,priority=10,udp6,in_port=%s,\
                        udp_dst=3784,action=output:%s,%s'" %
                        (br_name, dut_iface_id, vm_iface_id, injected_iface_id))
            # Add flow for BFD Control packets (UDP port 3784)
            bind_helper("ovs-ofctl add-flow %s 'table=0,priority=10,udp,in_port=%s,\
                        udp_src=49152,udp_dst=3784,action=output:%s,%s'" %
                        (br_name, dut_iface_id, vm_iface_id, injected_iface_id))
            bind_helper("ovs-ofctl add-flow %s 'table=0,priority=10,udp6,in_port=%s,\
                        udp_src=49152,udp_dst=3784,action=output:%s,%s'" %
                        (br_name, dut_iface_id, vm_iface_id, injected_iface_id))

        # Add flow from a ptf container to an external iface
            bind_helper("ovs-ofctl add-flow %s 'table=0,in_port=%s,action=output:%s'" %
                        (br_name, injected_iface_id, dut_iface_id))

            if is_batch_mode and all_cmds:
                processes = kwargs.get("processes")
                tmpdir = kwargs.get("tmpdir")
                with tempfile.NamedTemporaryFile("w", dir=tmpdir, delete=False) as f:
                    for rule in all_cmds:
                        f.write(rule.strip("'") + "\n")

                processes.append(VMTopology.fire_and_forget("ovs-ofctl add-flows {} {}".format(br_name, f.name)))

    def unbind_ovs_ports(self, br_name, vm_port, **kwargs):
        """unbind all ports except the vm port from an ovs bridge"""
        if VMTopology.intf_exists(br_name):
            ports = VMTopology.get_ovs_br_ports(br_name)

            bind_helper = VMTopology.cmd
            is_batch_mode = "processes" in kwargs

            all_cmds = []

            if is_batch_mode:
                bind_helper = lambda cmd: \
                    all_cmds.append(cmd[len("ovs-vsctl "):])  # noqa: E731

            for port in ports:
                if port != vm_port:
                    bind_helper('ovs-vsctl --if-exists del-port %s %s' % (br_name, port))

            if is_batch_mode and all_cmds:
                processes = kwargs.get("processes")
                batch_cmd = 'ovs-vsctl -- %s' % (' -- '.join(all_cmds))
                processes.append(VMTopology.fire_and_forget(batch_cmd))

    def unbind_ovs_port(self, br_name, port):
        """unbind a port from an ovs bridge"""
        if VMTopology.intf_exists(br_name):
            ports = VMTopology.get_ovs_br_ports(br_name)

            if port in ports:
                VMTopology.cmd('ovs-vsctl --if-exists del-port %s %s' % (br_name, port))

    def create_dualtor_cable(self, host_ifindex, host_if, upper_if, lower_if, active_if_index=0, nic_if=None):
        """
        create dualtor cable

        For the active/standby dualtor scenario, the OVS bridge is to simulate mux of y-cable.
                            +--------------+
                            |              +----- upper_if
            PTF (host_if) --+  OVS bridge  |
                            |              +----- lower_if
                            +--------------+

        For the active/active dualtor scenario, the OVS bridge is to simulator server smart NIC with two ports.
                            +--------------+
            PTF (host_if) --+              +----- upper_if
                            |  OVS bridge  |
            netns (ns_if) --+              +----- lower_if
                            +--------------+
        """

        br_name_template = MUXY_BRIDGE_TEMPLATE if nic_if is None else ACTIVE_ACTIVE_BRIDGE_TEMPLATE
        br_name = adaptive_name(
            br_name_template, self.vm_set_name, host_ifindex)

        self.create_ovs_bridge(br_name, self.fp_mtu)

        for intf in [host_if, upper_if, lower_if]:
            br = VMTopology.get_ovs_bridge_by_port(intf)
            if br is not None and br != br_name:
                VMTopology.cmd('ovs-vsctl --if-exists del-port %s %s' % (br, intf))

        ports = VMTopology.get_ovs_br_ports(br_name)
        ports_to_be_attached = [host_if, upper_if, lower_if]
        if nic_if is not None:
            ports_to_be_attached.append(nic_if)
        for intf in ports_to_be_attached:
            if intf not in ports:
                VMTopology.cmd('ovs-vsctl --may-exist add-port %s %s' % (br_name, intf))

        bridge_ports = [upper_if, lower_if]
        if nic_if is not None:
            bridge_ports.append(nic_if)
        bindings = VMTopology.get_ovs_port_bindings(br_name, bridge_ports)
        host_if_id = bindings[host_if]
        upper_if_id = bindings[upper_if]
        lower_if_id = bindings[lower_if]

        # clear old bindings
        VMTopology.cmd('ovs-ofctl del-flows %s' % br_name)

        if nic_if is not None:
            # TODO: open-flow configuration for ovs-bridge simulating server smart NIC
            pass
        else:
            # open-flow configuration for ovs-bridge simulating mux of dualtor y-cable
            VMTopology.cmd("ovs-ofctl add-flow %s table=0,in_port=%s,action=output:%s,%s" %
                           (br_name, host_if_id, upper_if_id, lower_if_id))
            if active_if_index == 0:
                VMTopology.cmd("ovs-ofctl add-flow %s table=0,in_port=%s,action=output:%s" %
                               (br_name, upper_if_id, host_if_id))
            else:
                VMTopology.cmd("ovs-ofctl add-flow %s table=0,in_port=%s,action=output:%s" %
                               (br_name, lower_if_id, host_if_id))

    def remove_dualtor_cable(self, host_ifindex, is_active_active=False):
        """
        remove muxy cable
        """
        br_template = ACTIVE_ACTIVE_BRIDGE_TEMPLATE if is_active_active else MUXY_BRIDGE_TEMPLATE
        br_name = adaptive_name(br_template, self.vm_set_name, host_ifindex)

        self.destroy_ovs_bridge(br_name)

    def add_host_ports(self):
        """
        add dut port in the ptf docker

        for non-dual topo, inject the dut port into ptf docker.
        for dual-tor topo, create ovs port and add to ptf docker.
        """
        def _add_host_port(i, intf):
            if self._is_multi_duts and not self._is_cable:
                if isinstance(intf, list):
                    # For dualtor interface: create veth link and inject one end into the ptf docker
                    # For active-active interface: create veth link and inject one end into the netns
                    # If host interface index is explicitly specified by "@x" (len(intf[0]==3), use host interface
                    # index specified in topo definition.
                    # Otherwise, it means that host interface does not have "@x" in topo definition, then assume that
                    # there is no gap in sequence of host interfaces.
                    host_ifindex = intf[0][2] if len(intf[0]) == 3 else i
                    is_active_active = intf in self.host_interfaces_active_active
                    dual_if_template = ACTIVE_ACTIVE_INTERFACES_TEMPLATE \
                        if is_active_active else MUXY_INTERFACES_TEMPLATE
                    dual_if = adaptive_name(
                        dual_if_template, self.vm_set_name, host_ifindex)
                    ptf_if = PTF_FP_IFACE_TEMPLATE % host_ifindex
                    self.add_veth_if_to_docker(dual_if, ptf_if)

                    if is_active_active:
                        nic_if = adaptive_name(
                            SERVER_NIC_INTERFACE_TEMPLATE, self.vm_set_name, host_ifindex)
                        ns_if = NETNS_IFACE_TEMPLATE % host_ifindex
                        self.add_veth_if_to_netns(nic_if, ns_if)
                        self.add_ip_to_netns_if(
                            ns_if, self.mux_cable_facts[host_ifindex]["soc_ipv4"])
                    else:
                        nic_if = None

                    upper_tor_if = self.duts_fp_ports[self.duts_name[intf[0][0]]][str(
                        intf[0][1])]
                    lower_tor_if = self.duts_fp_ports[self.duts_name[intf[1][0]]][str(
                        intf[1][1])]
                    # create muxy cable or active_active_cable for dualtor
                    self.create_dualtor_cable(
                        host_ifindex, dual_if, upper_tor_if, lower_tor_if, nic_if=nic_if)
                else:
                    host_ifindex = intf[2] if len(intf) == 3 else i
                    fp_port = self.duts_fp_ports[self.duts_name[intf[0]]][str(
                        intf[1])]
                    ptf_if = PTF_FP_IFACE_TEMPLATE % host_ifindex
                    self.add_dut_if_to_docker(ptf_if, fp_port)
            elif self._is_multi_duts and self._is_cable:
                # Since there could be multiple ToR's in cable topology, some Ports
                # can be connected to muxcable and some to a DAC cable. But it could
                # be possible that not all ports have cables connected. So for whichever
                # port link is connected and has a vlan associated, inject them to container
                # with the enumeration in topo file
                # essentially mux ports will map to one port and DAC ports will map to different
                # ports in a dualtor setup. Here implicit is taken that
                # interface index is explicitly specified by "@x" format
                host_ifindex = intf[0][2]
                if self.duts_fp_ports[self.duts_name[intf[0][0]]].get(str(intf[0][1])) is not None:
                    fp_port = self.duts_fp_ports[self.duts_name[intf[0][0]]][str(
                        intf[0][1])]
                    ptf_if = PTF_FP_IFACE_TEMPLATE % host_ifindex
                    self.add_dut_if_to_docker(ptf_if, fp_port)

                host_ifindex = intf[1][2]
                if self.duts_fp_ports[self.duts_name[intf[1][0]]].get(str(intf[1][1])) is not None:
                    fp_port = self.duts_fp_ports[self.duts_name[intf[1][0]]][str(
                        intf[1][1])]
                    ptf_if = PTF_FP_IFACE_TEMPLATE % host_ifindex
                    self.add_dut_if_to_docker(ptf_if, fp_port)
            else:
                fp_port = self.duts_fp_ports[self.duts_name[0]][str(intf)]
                ptf_if = PTF_FP_IFACE_TEMPLATE % intf
                self.add_dut_if_to_docker(ptf_if, fp_port)
                # only create sub interface for enabled ports defined in t0-backend
                if self.dut_type == BACKEND_TOR_TYPE and intf not in self.disabled_host_interfaces:
                    vlan_separator = self.topo.get("DUT", {}).get(
                        "sub_interface_separator", SUB_INTERFACE_SEPARATOR)
                    vlan_id = self.vlan_ids[str(intf)]
                    self.add_dut_vlan_subif_to_docker(
                        ptf_if, vlan_separator, vlan_id)

        self.worker.map(lambda args: _add_host_port(*args), enumerate(self.host_interfaces))

    def enable_netns_loopback(self):
        """Enable loopback device in the netns."""
        VMTopology.cmd("ip netns exec %s ifconfig lo up" % self.netns)

    def setup_netns_source_routing(self):
        """Setup policy-based routing to forward packet to its igress ports."""

        def get_existing_rt_tables():
            """Get existing routing tables."""
            rt_tables = {}
            with open(RT_TABLE_FILEPATH) as fd:
                for line in fd.readlines():
                    if line.startswith("#"):
                        continue
                    fields = line.split()
                    if fields and len(fields) == 2:
                        rt_tables[int(fields[0])] = fields[1]
            return rt_tables

        # NOTE: routing tables are visible to all network namespaces, but the route entries in one
        # routing table created in one network namespace are not visible to other network namespaces.
        # For the policy based routing applied to each netns, for each interface, there is a routing
        # table correspondinly with the same name. And this routing table could be shared across multiple
        # network namespaces, each network namespace has its own route entries stored on this routing
        # table.
        rt_tables = get_existing_rt_tables()
        slot_start_index = 100

        for i, intf in enumerate(self.host_interfaces):
            is_active_active = intf in self.host_interfaces_active_active
            if self._is_multi_duts and not self._is_cable and isinstance(intf, list) and is_active_active:
                host_ifindex = intf[0][2] if len(intf[0]) == 3 else i
                ns_if = NETNS_IFACE_TEMPLATE % host_ifindex
                if not VMTopology.intf_exists(ns_if, netns=self.netns):
                    raise RuntimeError(
                        "Interface %s not exists in netns %s" % (ns_if, self.netns))
                rt_slot = slot_start_index + int(host_ifindex)
                if rt_slot > 252:
                    raise RuntimeError(
                        "Kernel only supports up to 252 additional routing tables")
                rt_name = ns_if
                ns_if_addr = ipaddress.ip_interface(
                    six.ensure_text(self.mux_cable_facts[host_ifindex]["soc_ipv4"]))
                gateway_addr = str(ns_if_addr.network.network_address + 1)
                if rt_slot not in rt_tables:
                    # add route table mapping, use interface name as route table name
                    VMTopology.cmd("ip netns exec %s echo \"%s\t%s\n\" >> /etc/iproute2/rt_tables" %
                                   (self.netns, rt_slot, rt_name), shell=True, split_cmd=False)
                VMTopology.cmd("ip netns exec %s ip rule add iif %s table %s" % (
                    self.netns, ns_if, rt_name))
                VMTopology.cmd("ip netns exec %s ip rule add from %s table %s" % (
                    self.netns, ns_if_addr.ip, rt_name))
                # issue: https://www.mail-archive.com/debian-bugs-dist@lists.debian.org/msg1811241.html
                # When the route table is empty, the ip route flush command will fail.
                # So ignore the error here.
                VMTopology.cmd(
                    "ip netns exec %s ip route flush table %s" % (self.netns, rt_name), ignore_errors=True)
                VMTopology.cmd("ip netns exec %s ip route add %s dev %s table %s" % (
                    self.netns, ns_if_addr.network, ns_if, rt_name))
                VMTopology.cmd("ip netns exec %s ip route add default via %s dev %s table %s" % (
                    self.netns, gateway_addr, ns_if, rt_name))

    def remove_host_ports(self):
        """
        remove dut port from the ptf docker
        """
        logging.info("=== Remove host ports ===")

        def _remove_host_port(i, intf):
            if self._is_multi_duts:
                if isinstance(intf, list):
                    host_ifindex = intf[0][2] if len(intf[0]) == 3 else i
                    is_active_active = intf in self.host_interfaces_active_active
                    self.remove_dualtor_cable(
                        host_ifindex, is_active_active=is_active_active)
                else:
                    host_ifindex = intf[2] if len(intf) == 3 else i
                    fp_port = self.duts_fp_ports[self.duts_name[intf[0]]][str(
                        intf[1])]
                    ptf_if = PTF_FP_IFACE_TEMPLATE % host_ifindex
                    self.remove_dut_if_from_docker(ptf_if, fp_port)
            else:
                fp_port = self.duts_fp_ports[self.duts_name[0]][str(intf)]
                ptf_if = PTF_FP_IFACE_TEMPLATE % intf
                self.remove_dut_if_from_docker(ptf_if, fp_port)
                if self.dut_type == BACKEND_TOR_TYPE:
                    vlan_separator = self.topo.get("DUT", {}).get(
                        "sub_interface_separator", SUB_INTERFACE_SEPARATOR)
                    vlan_id = self.vlan_ids[str(intf)]
                    self.remove_dut_vlan_subif_from_docker(
                        ptf_if, vlan_separator, vlan_id)

        self.worker.map(lambda args: _remove_host_port(*args), enumerate(self.host_interfaces))

    def remove_veth_if_from_docker(self, ext_if, int_if, tmp_name):
        """
        Remove veth interface from docker
        """
        logging.info("=== Cleanup port, int_if: %s, ext_if: %s, tmp_name: %s ===" % (ext_if, int_if, tmp_name))
        if VMTopology.intf_exists(int_if, pid=self.pid):
            # Name it back to temp name in PTF container to avoid potential conflicts
            VMTopology.iface_down(int_if, pid=self.pid)
            VMTopology.cmd("nsenter -t %s -n ip link set dev %s name %s" % (self.pid, int_if, tmp_name))
            # Set it to default namespace
            VMTopology.cmd("nsenter -t %s -n ip link set dev %s netns 1" % (self.pid, tmp_name))

        # Delete its peer in default namespace
        if VMTopology.intf_exists(ext_if):
            VMTopology.cmd("ip link delete dev %s || true" % ext_if, shell=True, split_cmd=False)

    def remove_ptf_mgmt_port(self):
        ext_if = PTF_MGMT_IF_TEMPLATE % self.vm_set_name
        tmp_name = MGMT_PORT_NAME + VMTopology._generate_fingerprint(ext_if, MAX_INTF_LEN - len(MGMT_PORT_NAME))
        self.remove_veth_if_from_docker(ext_if, MGMT_PORT_NAME, tmp_name)

    def remove_ptf_backplane_port(self):
        ext_if = PTF_BP_IF_TEMPLATE % self.vm_set_name
        tmp_name = BP_PORT_NAME + VMTopology._generate_fingerprint(ext_if, MAX_INTF_LEN - len(BP_PORT_NAME))
        self.remove_veth_if_from_docker(ext_if, BP_PORT_NAME, tmp_name)

    def remove_injected_fp_ports_from_docker(self):
        for vm, vlans in self.injected_fp_ports.items():
            for vlan in vlans:
                (_, _, ptf_index) = VMTopology.parse_vm_vlan_port(vlan)
                ext_if = adaptive_name(INJECTED_INTERFACES_TEMPLATE, self.vm_set_name, ptf_index)
                int_if = PTF_FP_IFACE_TEMPLATE % ptf_index
                properties = self.vm_properties.get(vm, {})
                create_vlan_subintf = properties.get('device_type') in (
                    BACKEND_TOR_TYPE, BACKEND_LEAF_TYPE)
                if not create_vlan_subintf:
                    tmp_name = int_if + VMTopology._generate_fingerprint(ext_if, MAX_INTF_LEN - len(int_if))
                    self.remove_veth_if_from_docker(ext_if, int_if, tmp_name)

    @staticmethod
    def _generate_fingerprint(name, digit=6):
        """
            Generate fingerprint
            Args:
                name (str): name
                digit (int): digit of fingerprint, e.g. 6

            Returns:
                str: fingerprint, e.g. a9d24d
            """
        return hashlib.md5(name.encode("utf-8")).hexdigest()[0:digit]

    @staticmethod
    def _intf_cmd(intf, pid=None, netns=None):
        if pid:
            cmdline = 'nsenter -t %s -n ifconfig -a %s' % (pid, intf)
        elif netns:
            cmdline = 'ip netns exec %s ifconfig -a %s' % (netns, intf)
        else:
            cmdline = 'ifconfig -a %s' % intf
        return cmdline

    @staticmethod
    def intf_exists(intf, pid=None, netns=None):
        """Check if the specified interface exists.

        This function uses command "ifconfig <intf name>" to check the existence of the specified interface. By default
        the command is executed on host. If a pid is specified, this command is executed in the network namespace
        of the specified pid. The meaning is to check if the interface exists in a specific docker.
        If a netns is specified, this command is executed in the specified network namespace. The specified network
        namespace is not a docker container. It is a network namespace created using the "ip netns" command.
        The both pip and netns arguments are specified, the pid argument takes precedence.

        Args:
            intf (str): Name of the interface.
            pid (str), optional): Pid of docker. Defaults to None.
            netns (str), optional): netns name. Default to None.

        Returns:
            bool: True if the interface exists. Otherwise False.
        """
        cmdline = VMTopology._intf_cmd(intf, pid=pid, netns=netns)

        try:
            VMTopology.cmd(cmdline, retry=3)
            return True
        except Exception:
            return False

    @staticmethod
    def intf_not_exists(intf, pid=None, netns=None):
        """Check if the specified interface does not exist.

        This function uses command "ifconfig <intf name>" to check the existence of the specified interface. By default
        the command is executed on host. If a pid is specified, this command is executed in the network namespace
        of the specified pid. The meaning is to check if the interface exists in a specific docker.
        If a netns is specified, this command is executed in the specified network namespace. The specified network
        namespace is not a docker container. It is a network namespace created using the "ip netns" command.
        The both pip and netns arguments are specified, the pid argument takes precedence.

        Args:
            intf (str): Name of the interface.
            pid (str), optional): Pid of docker. Defaults to None.
            netns (str), optional): netns name. Default to None.

        Returns:
            bool: True if the interface does not exist. Otherwise False.
        """
        cmdline = VMTopology._intf_cmd(intf, pid=pid, netns=netns)

        try:
            VMTopology.cmd(cmdline, retry=3, negative=True)
            return True
        except Exception:
            return False

    @staticmethod
    def _ip_cmd(intf, pid=None, netns=None, ipv6=False):
        addr_cmd = 'ip addr show'
        if ipv6:
            addr_cmd = 'ip -6 addr show'

        if pid:
            cmdline = 'nsenter -t %s -n %s dev %s' % (pid, addr_cmd, intf)
        elif netns:
            cmdline = 'ip netns exec %s %s dev %s' % (netns, addr_cmd, intf)
        else:
            cmdline = '%s dev %s' % (addr_cmd, intf)
        return cmdline

    @staticmethod
    def ip_exists(intf, ip_addr, pid=None, netns=None, ipv6=False):
        cmdline = VMTopology._ip_cmd(intf, pid=pid, netns=netns, ipv6=ipv6)

        try:
            output = VMTopology.cmd(cmdline, retry=3)
            return ip_addr in output
        except Exception:
            return False

    @staticmethod
    def _route_cmd(pid=None, netns=None, ipv6=False):
        addr_cmd = 'ip route show default'
        if ipv6:
            addr_cmd = 'ip -6 route show default'

        if pid:
            cmdline = 'nsenter -t %s -n %s' % (pid, addr_cmd)
        elif netns:
            cmdline = 'ip netns exec %s %s' % (netns, addr_cmd)
        else:
            cmdline = '%s' % (addr_cmd)
        return cmdline

    @staticmethod
    def route_exists(gw, pid=None, netns=None, ipv6=False):
        cmdline = VMTopology._route_cmd(pid=pid, netns=netns, ipv6=ipv6)

        try:
            output = VMTopology.cmd(cmdline, retry=3)
            return gw in output
        except Exception:
            return False

    @staticmethod
    def iface_up(iface_name, pid=None, netns=None):
        return VMTopology.iface_updown(iface_name, 'up', pid, netns)

    @staticmethod
    def iface_down(iface_name, pid=None, netns=None):
        return VMTopology.iface_updown(iface_name, 'down', pid, netns)

    @staticmethod
    def iface_updown(iface_name, state, pid, netns):
        if pid is not None:
            return VMTopology.cmd('nsenter -t %s -n ip link set %s %s' % (pid, iface_name, state))
        elif netns is not None:
            return VMTopology.cmd('ip netns exec %s ip link set %s %s' % (netns, iface_name, state))
        else:
            return VMTopology.cmd('ip link set %s %s || true' % (iface_name, state), shell=True, split_cmd=False)

    @staticmethod
    def iface_disable_txoff(iface_name, pid=None):
        if pid is None:
            return VMTopology.cmd('ethtool -K %s tx off' % (iface_name))
        else:
            return VMTopology.cmd('nsenter -t %s -n ethtool -K %s tx off' % (pid, iface_name))

    @staticmethod
    def fire_and_forget(cmdline):
        cmdline_ori = cmdline
        cmdline = shlex.split(cmdline_ori)

        return VMTopologyWorker.Popen(
                cmdline,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False)

    @staticmethod
    def cmd(cmdline, grep_cmd=None, retry=1, negative=False, shell=False, split_cmd=True, ignore_errors=False):
        """Execute a command and return the output

        Args:
            cmdline (str): The command line to be executed.
            grep_cmd (str, optional): Grep command line. Defaults to None.
            retry (int, optional): Max number of retry if command result is unexpected. Defaults to 1.
            negative (bool, optional): If negative is True, expect the command to fail. Defaults to False.
            ignore_errors (bool, optional): If ignore_errors is True, return the output even if the command fails.

        Raises:
            Exception: If command result is unexpected after max number of retries, raise an exception.

        Returns:
            str: Output of the command.
        """

        cmdline_ori = cmdline
        grep_cmd_ori = grep_cmd
        for attempt in range(retry):
            logging.debug('*** CMD: %s, grep: %s, attempt: %d' %
                          (cmdline, grep_cmd, attempt + 1))
            if split_cmd:
                cmdline = shlex.split(cmdline_ori)
            process = subprocess.Popen(
                cmdline,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=shell)
            if grep_cmd:
                if split_cmd:
                    grep_cmd = shlex.split(grep_cmd_ori)
                process_grep = subprocess.Popen(
                    grep_cmd,
                    stdin=process.stdout,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    shell=shell)
                out, err = process_grep.communicate()
                ret_code = process_grep.returncode
            else:
                out, err = process.communicate()
                ret_code = process.returncode
            out, err = out.decode('utf-8'), err.decode('utf-8')

            msg = {
                'cmd': cmdline,
                'grep_cmd': grep_cmd,
                'ret_code': ret_code,
                'stdout': out.splitlines(),
                'stderr': err.splitlines()
            }
            logging.debug('*** OUTPUT: \n%s' % json.dumps(msg, indent=2))

            if negative:
                if ret_code != 0:
                    # Result is expected, return early
                    return out
                else:
                    # Result is unexpected, need to retry
                    continue
            else:
                if ret_code == 0:
                    # Result is expected, return early
                    return out
                else:
                    # Result is unexpected, need to retry
                    continue

        if ignore_errors:
            return out
        else:
            # Reached max retry, fail with exception
            err_msg = 'ret_code=%d, error message="%s". cmd="%s%s"' \
                % (ret_code, err, cmdline_ori, ' | ' + grep_cmd_ori if grep_cmd_ori else '')
            raise Exception(err_msg)

    @staticmethod
    def get_ovs_br_ports(bridge):
        out = VMTopology.cmd('ovs-vsctl list-ports %s || true' % bridge, shell=True, split_cmd=False)
        ports = set()
        for port in out.split('\n'):
            if port != "":
                ports.add(port)
        return ports

    @staticmethod
    def get_ovs_bridge_by_port(port):
        try:
            out = VMTopology.cmd('ovs-vsctl port-to-br %s' % port)
        except Exception:
            return None

        bridge = out.rstrip()
        return bridge

    @staticmethod
    def get_ovs_port_bindings(bridge, vlan_iface=[]):
        # Vlan interface addition may take few secs to reflect in OVS Command,
        # Let`s retry few times in that case.
        for retries in range(RETRIES):
            out = VMTopology.cmd('ovs-ofctl show %s' % bridge)
            lines = out.split('\n')
            result = {}
            for line in lines:
                matched = re.match(r'^\s+(\S+)\((\S+)\):\s+addr:.+$', line)
                if matched:
                    port_id = matched.group(1)
                    iface_name = matched.group(2)
                    result[iface_name] = port_id
            # Check if we have vlan_iface populated
            if len(vlan_iface) == 0 or all([intf in result for intf in vlan_iface]):
                return result
            time.sleep(2 * retries + 1)
        # Flow reaches here when vlan_iface not present in result
        raise Exception("Can't find vlan_iface_id")

    @staticmethod
    def get_pid(ptf_name):
        cli = docker.from_env()
        try:
            ctn = cli.containers.get(ptf_name)
        except Exception:
            return None

        if ctn.attrs['State']['Running']:
            # If the container is running, return its PID
            return ctn.attrs['State']['Pid']
        else:
            # If the container is not running, return None
            logging.error('!!! Container %s is not running' % ptf_name)
            return None

    @staticmethod
    def brctl_show(bridge=None):
        br_to_ifs = {}
        if_to_br = {}

        cmdline = "brctl show "
        if bridge:
            cmdline += bridge
        try:
            out = VMTopology.cmd(cmdline)
        except Exception:
            logging.error('!!! Failed to run %s' % cmdline)
            return br_to_ifs, if_to_br

        rows = out.split('\n')[1:]
        cur_br = None
        for row in rows:
            if len(row) == 0:
                continue
            terms = row.split()
            if not row[0].isspace():
                cur_br = terms[0]
                br_to_ifs[cur_br] = []
                if len(terms) > 3:
                    br_to_ifs[cur_br].append(terms[3])
                    if_to_br[terms[3]] = cur_br
            else:
                br_to_ifs[cur_br].append(terms[0])
                if_to_br[terms[0]] = cur_br

        return br_to_ifs, if_to_br

    @staticmethod
    def parse_vm_vlan_port(vlan):
        """
        parse vm vlan port

        old format (non multi-dut): vlan_index
        new format (multi-dut):     dut_index.vlan_index@ptf_index

        """
        if isinstance(vlan, int):
            dut_index = 0
            vlan_index = vlan
            ptf_index = vlan
        else:
            m = re.match(r"(\d+)\.(\d+)@(\d+)", vlan)
            (dut_index, vlan_index, ptf_index) = (
                int(m.group(1)), int(m.group(2)), int(m.group(3)))

        return (dut_index, vlan_index, ptf_index)


def check_topo(topo, is_multi_duts=False):

    def _assert(condition, exctype, msg):
        if not condition:
            raise exctype(msg)

    hostif_exists = False
    vms_exists = False
    all_intfs = set()

    if 'host_interfaces' in topo:
        host_interfaces = topo['host_interfaces']

        _assert(isinstance(host_interfaces, list), TypeError,
                "topo['host_interfaces'] should be a list")

        for host_intf in host_interfaces:
            if is_multi_duts:
                for p in host_intf.split(','):
                    condition = (isinstance(p, str) and
                                 re.match(r"^\d+\.\d+(@\d+)?$", p))
                    _assert(condition, ValueError,
                            "topo['host_interfaces'] should be a "
                            "list of strings of format '<dut>.<dut_intf>' or '<dut>.<dut_intf>,<dut>.<dut_intf>'")
                    _assert(p not in all_intfs, ValueError,
                            "topo['host_interfaces'] double use of host interface: %s" % p)
                    all_intfs.add(p)
            else:
                condition = isinstance(host_intf, int) and host_intf >= 0
                _assert(condition, ValueError,
                        "topo['host_interfaces'] should be a "
                        "list of positive integers")
                _assert(host_intf not in all_intfs, ValueError,
                        "topo['host_interfaces'] double use of host interface: %s" % host_intf)
                all_intfs.add(host_intf)

        hostif_exists = True

    if 'VMs' in topo:
        VMs = topo['VMs']

        _assert(isinstance(VMs, dict), TypeError,
                "topo['VMs'] should be a dictionary")

        for hostname, attrs in VMs.items():
            _assert('vlans' in attrs and isinstance(attrs['vlans'], list),
                    ValueError,
                    "topo['VMs']['%s'] should contain "
                    "'vlans' with a list of vlans" % hostname)

            _assert(('vm_offset' in attrs and
                     isinstance(attrs['vm_offset'], int)),
                    ValueError,
                    "topo['VMs']['%s'] should contain "
                    "'vm_offset' with a number" % hostname)

            for vlan in attrs['vlans']:
                if is_multi_duts:
                    condition = (isinstance(vlan, str) and
                                 re.match(r"^\d+\.\d+(@\d+)?$", vlan))
                    _assert(condition, ValueError,
                            "topo['VMs'][%s]['vlans'] should be "
                            "list of strings of format '<dut>.<vlan>'. vlan=%s" % (hostname, vlan))
                else:
                    _assert(isinstance(vlan, int) and vlan >= 0,
                            ValueError,
                            "topo['VMs'][%s]['vlans'] should contain"
                            " a list with integers. vlan=%s" % (hostname, vlan))
                _assert(vlan not in all_intfs,
                        ValueError,
                        "topo['VMs'][%s]['vlans'] double use "
                        "of vlan: %s" % (hostname, vlan))
                all_intfs.add(vlan)

        vms_exists = True

    return hostif_exists, vms_exists


def check_devices_interconnect(topo, is_mutli_dut=False):
    def _assert(condition, exctype, msg):
        if not condition:
            raise exctype(msg)

    devices_interconnect_exists = False
    all_vlans = set()
    if 'devices_interconnect_interfaces' in topo:
        links = topo['devices_interconnect_interfaces']
        for key, vlans in links.items():
            for vlan in vlans:
                if is_mutli_dut:
                    condition = (isinstance(vlan, str) and re.match(
                        r"^\d+\.\d+(@\d+)?$", vlan))
                    _assert(condition, ValueError,
                            "topo['devices_interconnect_interfaces'][%s] should be a "
                            "list of strings of format '<dut>.<dut_intf>' or '<dut>.<dut_intf>,<dut>.<dut_intf>'")
                else:
                    _assert(isinstance(vlan, int) and vlan >= 0, ValueError,
                            "topo['devices_interconnect_interfaces'][%s] should be a list of integers" % key)
                _assert(vlan not in all_vlans, ValueError,
                        "topo['devices_interconnect_interfaces'][%s] double use of vlan: %s" % (key, vlan))
                all_vlans.add(vlan)
        devices_interconnect_exists = True
    return devices_interconnect_exists


def check_params(module, params, mode):
    for param in params:
        if param not in module.params:
            raise Exception("Parameter %s is required in %s mode" %
                            (param, mode))


class ThreadBufferHandler(logging.Handler):
    """
    ThreadBufferHandler stores log records from each thread separately and can flush
    logs from each thread separately.

    Each thread will have its own memory log handler, and each log will be only buffered in
    the memory log handler of the thread that emits the log. The flushing is performed by
    each memory log handler whenever the memory buffer is full or explicitly triggered by
    user. The logs from one thread will be coalesced together and batch-sent to the target
    handler.
    """

    THREAD_LOG_HANDLER_CAPACITY = 4096

    def __init__(self, target, loglevel=logging.NOTSET):
        """
        Initialize the ThreadBufferHandler object.

        Args:
            target: the target handler, all log records stored temporarily in this handler will be
                    flushed to the target handler.
            loglevel: log level.
        """
        super(ThreadBufferHandler, self).__init__(level=loglevel)
        self.memory_handlers = {}
        self.target = target

    def get_current_thread_log_memory_handler(self):
        """Get the current thread log memory handler."""
        thread_id = threading.current_thread().ident
        if thread_id in self.memory_handlers:
            return self.memory_handlers[thread_id]
        else:
            memory_handler = MemoryHandler(ThreadBufferHandler.THREAD_LOG_HANDLER_CAPACITY,
                                           target=self.target)
            self.memory_handlers[thread_id] = memory_handler
            return memory_handler

    def flush_current_thread_logs(self):
        """Flush the log records stored in the current thread log memory handler."""
        self.get_current_thread_log_memory_handler().flush()

    def emit(self, record):
        """
        Emit a record.

        Dispatch the log record to the current thread log memory handler.
        """
        self.get_current_thread_log_memory_handler().emit(record)

    def flush(self):
        """Flush all log records to the target handler."""
        for handler in self.memory_handlers.values():
            handler.flush()
        self.target.flush()

    def close(self):
        """Close all log memory handlers."""
        for handler in self.memory_handlers.values():
            handler.close()
        self.memory_handlers.clear()
        self.target.close()
        super(ThreadBufferHandler, self).close()


class VMTopologyWorker(object):
    """VM Topology worker class."""

    def __init__(self, use_thread_worker, thread_worker_count):
        """
        Initialize the VMTopologyWorker object.

        Args:
            use_thread_worker: use thread pool or not.
            thread_worker_count: the thread worker count if use thread pool is enabled.
        """
        logging.info("Init VM topology worker: use thread worker %s, thread worker count %s",
                     use_thread_worker, thread_worker_count)
        self.thread_pool = None
        self._map_helper = map
        self._shutdown_helper = None
        self.use_thread_worker = use_thread_worker
        self.thread_worker_count = thread_worker_count
        self.thread_buffer_handler = None
        if use_thread_worker:
            self.thread_pool = ThreadPool(thread_worker_count)
            self._map_helper = self.thread_pool.map
            if hasattr(self.thread_pool, "shutdown"):
                if sys.version_info >= (3, 9):
                    self._shutdown_helper = lambda: self.thread_pool.shutdown(wait=True, cancel_futures=True)
                else:
                    self._shutdown_helper = lambda: self.thread_pool.shutdown(wait=True)
            else:
                self._shutdown_helper = \
                    lambda: self.thread_pool.terminate()

            self._setup_thread_buffered_handler()

    def _setup_thread_buffered_handler(self):
        """Setup the per-thread log batch handler with ThreadBufferHandler."""
        handlers = logging.getLogger().handlers
        if not handlers:
            raise ValueError("No logging handler is available in the default logging.")
        handler = handlers[-1]
        self.thread_buffer_handler = ThreadBufferHandler(target=handler)

    def map(self, func, iterable):
        """Apply the function to every item of the iterable."""
        def _buffer_logs_helper(func, *args, **kwargs):
            if self.use_thread_worker:
                logging.debug(LOG_SEPARATOR)
                logging.debug("Start task %s, arguments (%s, %s), worker %s",
                              func, args, kwargs, threading.current_thread().ident)
            try:
                func(*args, **kwargs)
            finally:
                if self.use_thread_worker:
                    logging.debug("Finish task %s, arguments (%s, %s), worker %s",
                                  func, args, kwargs, threading.current_thread().ident)
                    logging.debug(LOG_SEPARATOR)
                    self.thread_buffer_handler.flush_current_thread_logs()

        # NOTE: replace the original handler with the thread buffer handler, so logs from
        # one task will be buffered and flushed together.
        if self.use_thread_worker:
            handlers = logging.getLogger().handlers
            handlers.remove(self.thread_buffer_handler.target)
            handlers.append(self.thread_buffer_handler)
        try:
            return list(self._map_helper(functools.partial(_buffer_logs_helper, func), iterable))
        finally:
            if self.use_thread_worker:
                handlers.remove(self.thread_buffer_handler)
                handlers.append(self.thread_buffer_handler.target)

    def shutdown(self):
        """Stop the worker threads immediately without completing outstanding work."""
        if self.use_thread_worker:
            self._shutdown_helper()
            self.thread_buffer_handler.flush()

    def __del__(self):
        self.shutdown()

    @staticmethod
    @contextmanager
    def safe_subprocess_manager(timeout=DEFAULT_BATCH_PROCESSES_TIMEOUT):
        tmpdir = tempfile.mkdtemp(prefix="/tmp/")
        processes = []
        err_msgs = []

        yield processes, tmpdir

        for process in processes:
            if sys.version_info.major < 3:
                # Python 2: implement manual timeout
                start_time = time.time()
                while process.poll() is None:
                    if time.time() - start_time > timeout:
                        process.kill()
                        out, err = process.communicate()
                        raise Exception("Process timeout after {} seconds".format(timeout))
                    time.sleep(1)
                out, err = process.communicate()
            else:
                out, err = process.communicate(timeout=timeout)

            out, err = out.decode('utf-8'), err.decode('utf-8')
            return_code = process.returncode

            if return_code != 0:
                err_msg = "return_code={}, error message='{}', cmd='{}'".format(return_code, err, process.args)
                err_msgs.append(err_msg)

        shutil.rmtree(tmpdir)

        if len(err_msgs) > 0:
            raise Exception(json.dumps({'message': 'One of the batch commands failed', 'error': err_msg}, indent=2))

    @staticmethod
    def Popen(*args, **kwds):
        res = subprocess.Popen(*args, **kwds)

        if not hasattr(res, "args"):
            res.args = args[0]

        return res


def main():
    module = AnsibleModule(
        argument_spec=dict(
            cmd=dict(required=True, choices=['create', 'bind', 'bind_keysight_api_server_ip',
                     'renumber', 'unbind', 'destroy', "connect-vms", "disconnect-vms"]),
            vm_set_name=dict(required=False, type='str'),
            topo=dict(required=False, type='dict'),
            vm_names=dict(required=True, type='list'),
            current_vm_name=dict(required=False, type='str'),
            vm_base=dict(required=False, type='str'),
            vm_type=dict(required=False, type='str'),
            vm_properties=dict(required=False, type='dict', default={}),
            ptf_mgmt_ip_addr=dict(required=False, type='str'),
            ptf_mgmt_ipv6_addr=dict(required=False, type='str'),
            ptf_mgmt_ip_gw=dict(required=False, type='str'),
            ptf_mgmt_ipv6_gw=dict(required=False, type='str'),
            ptf_extra_mgmt_ip_addr=dict(required=False, type='list', default=[]),
            ptf_bp_ip_addr=dict(required=False, type='str'),
            ptf_bp_ipv6_addr=dict(required=False, type='str'),
            mgmt_bridge=dict(required=False, type='str'),
            duts_fp_ports=dict(required=False, type='dict'),
            duts_mgmt_port=dict(required=False, type='list'),
            duts_midplane_ports=dict(required=False, type='dict', default={}),
            duts_inband_ports=dict(required=False, type='dict', default={}),
            duts_name=dict(required=False, type='list'),
            dut_interfaces=dict(required=False, type='str'),
            fp_mtu=dict(required=False, type='int', default=DEFAULT_MTU),
            max_fp_num=dict(required=False, type='int',
                            default=NUM_FP_VLANS_PER_FP),
            netns_mgmt_ip_addr=dict(required=False, type='str', default=None),
            is_dpu=(dict(required=False, type='bool', default=False)),
            is_vs_chassis=(dict(required=False, type='bool', default=False)),
            use_thread_worker=dict(required=False, type='bool', default=True),
            thread_worker_count=dict(required=False, type='int',
                                     default=max(MIN_THREAD_WORKER_COUNT,
                                                 multiprocessing.cpu_count() // 8)),
            batch_mode=dict(required=False, type='bool', default=False)
        ),
        supports_check_mode=False)

    cmd = module.params['cmd']
    vm_set_name = module.params['vm_set_name']
    vm_names = module.params['vm_names']
    current_vm_name = module.params['current_vm_name']
    fp_mtu = module.params['fp_mtu']
    max_fp_num = module.params['max_fp_num']
    vm_properties = module.params['vm_properties']
    is_dpu = module.params['is_dpu'] if 'is_dpu' in module.params else False
    is_vs_chassis = module.params['is_vs_chassis']
    dut_interfaces = module.params['dut_interfaces']
    use_thread_worker = module.params['use_thread_worker']
    thread_worker_count = module.params['thread_worker_count']
    batch_mode = module.params['batch_mode']

    config_module_logging(construct_log_filename(cmd, vm_set_name))

    if cmd == 'bind_keysight_api_server_ip':
        vm_names = []

    try:
        topo = module.params['topo']
        worker = VMTopologyWorker(use_thread_worker, thread_worker_count)
        net = VMTopology(vm_names, vm_properties, fp_mtu, max_fp_num, topo, worker, current_vm_name,
                         is_dpu, is_vs_chassis, dut_interfaces)

        if cmd == 'create':
            net.create_bridges()
        elif cmd == 'destroy':
            net.destroy_bridges()
        elif cmd == 'bind':
            check_params(module, ['vm_set_name',
                                  'topo',
                                  'ptf_mgmt_ip_addr',
                                  'ptf_mgmt_ipv6_addr',
                                  'ptf_mgmt_ip_gw',
                                  'ptf_mgmt_ipv6_gw',
                                  'ptf_extra_mgmt_ip_addr',
                                  'ptf_bp_ip_addr',
                                  'ptf_bp_ipv6_addr',
                                  'mgmt_bridge',
                                  'duts_fp_ports'], cmd)

            vm_set_name = module.params['vm_set_name']
            duts_fp_ports = module.params['duts_fp_ports']
            duts_midplane_ports = module.params['duts_midplane_ports']
            duts_inband_ports = module.params['duts_inband_ports']
            duts_name = module.params['duts_name']
            is_multi_duts = True if len(duts_name) > 1 else False

            if len(vm_set_name) > VM_SET_NAME_MAX_LEN:
                raise Exception("vm_set_name can't be longer than %d characters: %s (%d)" % (
                    VM_SET_NAME_MAX_LEN, vm_set_name, len(vm_set_name)))

            hostif_exists, vms_exists = check_topo(topo, is_multi_duts)
            devices_interconnect_exists = check_devices_interconnect(
                topo, is_multi_duts)

            if vms_exists:
                check_params(module, ['vm_base'], cmd)
                vm_base = module.params['vm_base']
            else:
                vm_base = None

            net.init(vm_set_name, vm_base, duts_fp_ports, duts_name)

            ptf_mgmt_ip_addr = module.params['ptf_mgmt_ip_addr']
            ptf_mgmt_ipv6_addr = module.params['ptf_mgmt_ipv6_addr']
            ptf_mgmt_ip_gw = module.params['ptf_mgmt_ip_gw']
            ptf_mgmt_ipv6_gw = module.params['ptf_mgmt_ipv6_gw']
            ptf_extra_mgmt_ip_addr = module.params['ptf_extra_mgmt_ip_addr']
            mgmt_bridge = module.params['mgmt_bridge']
            netns_mgmt_ip_addr = module.params['netns_mgmt_ip_addr']

            # Add management port to PTF docker and configure IP
            net.add_mgmt_port_to_docker(mgmt_bridge, ptf_mgmt_ip_addr, ptf_mgmt_ip_gw,
                                        ptf_mgmt_ipv6_addr, ptf_mgmt_ipv6_gw, ptf_extra_mgmt_ip_addr)

            ptf_bp_ip_addr = module.params['ptf_bp_ip_addr']
            ptf_bp_ipv6_addr = module.params['ptf_bp_ipv6_addr']

            if module.params['duts_mgmt_port']:
                for dut_mgmt_port in module.params['duts_mgmt_port']:
                    if dut_mgmt_port != "":
                        # For VS setup
                        net.bind_mgmt_port(mgmt_bridge, dut_mgmt_port)

            if vms_exists:
                net.add_injected_fp_ports_to_docker()
                net.add_injected_VM_ports_to_docker()
                net.bind_fp_ports(batch_mode=batch_mode)
                net.bind_vm_backplane()
                net.add_bp_port_to_docker(ptf_bp_ip_addr, ptf_bp_ipv6_addr)
                if is_vs_chassis:
                    net.bind_vs_chassis_ports(duts_midplane_ports, duts_inband_ports)

            if net.netns:
                net.add_network_namespace()
                # Let's enable arp_filter in the netns
                # to prevent arp flux
                net.enable_arp_filter_netns()
                net.add_mgmt_port_to_netns(
                    mgmt_bridge, netns_mgmt_ip_addr, ptf_mgmt_ip_gw)
                net.enable_netns_loopback()

            if hostif_exists:
                net.add_host_ports()

            if net.netns:
                net.setup_netns_source_routing()

            if devices_interconnect_exists:
                net.bind_devices_interconnect()

        elif cmd == 'bind_keysight_api_server_ip':
            check_params(module, ['ptf_mgmt_ip_addr',
                                  'ptf_mgmt_ipv6_addr',
                                  'ptf_mgmt_ip_gw',
                                  'ptf_mgmt_ipv6_gw',
                                  'ptf_extra_mgmt_ip_addr',
                                  'mgmt_bridge'], cmd)

            ptf_mgmt_ip_addr = module.params['ptf_mgmt_ip_addr']
            ptf_mgmt_ipv6_addr = module.params['ptf_mgmt_ipv6_addr']
            ptf_mgmt_ip_gw = module.params['ptf_mgmt_ip_gw']
            ptf_mgmt_ipv6_gw = module.params['ptf_mgmt_ipv6_gw']
            ptf_extra_mgmt_ip_addr = module.params['ptf_extra_mgmt_ip_addr']
            mgmt_bridge = module.params['mgmt_bridge']

            api_server_pid = net.get_pid('apiserver')

            net.add_mgmt_port_to_docker(mgmt_bridge, ptf_mgmt_ip_addr, ptf_mgmt_ip_gw,
                                        ptf_mgmt_ipv6_addr, ptf_mgmt_ipv6_gw, ptf_extra_mgmt_ip_addr, api_server_pid)
        elif cmd == 'unbind':
            check_params(module, ['vm_set_name',
                                  'topo',
                                  'duts_fp_ports'], cmd)

            vm_set_name = module.params['vm_set_name']
            topo = module.params['topo']
            duts_fp_ports = module.params['duts_fp_ports']
            duts_midplane_ports = module.params['duts_midplane_ports']
            duts_inband_ports = module.params['duts_inband_ports']
            duts_name = module.params['duts_name']
            is_multi_duts = True if len(duts_name) > 1 else False

            if len(vm_set_name) > VM_SET_NAME_MAX_LEN:
                raise Exception("vm_set_name can't be longer than %d characters: %s (%d)" % (
                    VM_SET_NAME_MAX_LEN, vm_set_name, len(vm_set_name)))

            hostif_exists, vms_exists = check_topo(topo, is_multi_duts)
            devices_interconnect_exists = check_devices_interconnect(
                topo, is_multi_duts)

            if vms_exists:
                check_params(module, ['vm_base'], cmd)
                vm_base = module.params['vm_base']
            else:
                vm_base = None

            net.init(vm_set_name, vm_base, duts_fp_ports,
                     duts_name, check_bridge=False)

            if module.params['duts_mgmt_port']:
                for dut_mgmt_port in module.params['duts_mgmt_port']:
                    if dut_mgmt_port != "":
                        net.unbind_mgmt_port(dut_mgmt_port)

            if vms_exists:
                net.unbind_vm_backplane()
                net.unbind_fp_ports(batch_mode=batch_mode)
                net.remove_injected_fp_ports_from_docker()
                if is_vs_chassis:
                    net.unbind_vs_chassis_ports(duts_midplane_ports, duts_inband_ports)

            if hostif_exists:
                net.remove_host_ports()

            net.remove_ptf_mgmt_port()
            net.remove_ptf_backplane_port()

            if net.netns:
                net.unbind_mgmt_port(NETNS_MGMT_IF_TEMPLATE % net.vm_set_name)
                net.delete_network_namespace()

            if devices_interconnect_exists:
                net.unbind_devices_interconnect()

        elif cmd == 'renumber':
            check_params(module, ['vm_set_name',
                                  'topo',
                                  'ptf_mgmt_ip_addr',
                                  'ptf_mgmt_ipv6_addr',
                                  'ptf_mgmt_ip_gw',
                                  'ptf_mgmt_ipv6_gw',
                                  'ptf_extra_mgmt_ip_addr',
                                  'ptf_bp_ip_addr',
                                  'ptf_bp_ipv6_addr',
                                  'mgmt_bridge',
                                  'duts_fp_ports'], cmd)

            vm_set_name = module.params['vm_set_name']
            topo = module.params['topo']
            duts_fp_ports = module.params['duts_fp_ports']
            duts_name = module.params['duts_name']
            is_multi_duts = True if len(duts_name) > 1 else False

            if len(vm_set_name) > VM_SET_NAME_MAX_LEN:
                raise Exception("vm_set_name can't be longer than %d characters: %s (%d)" % (
                    VM_SET_NAME_MAX_LEN, vm_set_name, len(vm_set_name)))

            hostif_exists, vms_exists = check_topo(topo, is_multi_duts)
            devices_interconnect_exists = check_devices_interconnect(
                topo, is_multi_duts)

            if vms_exists:
                check_params(module, ['vm_base'], cmd)
                vm_base = module.params['vm_base']
            else:
                vm_base = None

            net.init(vm_set_name, vm_base, duts_fp_ports, duts_name, True)

            ptf_mgmt_ip_addr = module.params['ptf_mgmt_ip_addr']
            ptf_mgmt_ipv6_addr = module.params['ptf_mgmt_ipv6_addr']
            ptf_mgmt_ip_gw = module.params['ptf_mgmt_ip_gw']
            ptf_mgmt_ipv6_gw = module.params['ptf_mgmt_ipv6_gw']
            ptf_extra_mgmt_ip_addr = module.params['ptf_extra_mgmt_ip_addr']
            mgmt_bridge = module.params['mgmt_bridge']
            netns_mgmt_ip_addr = module.params['netns_mgmt_ip_addr']

            net.add_mgmt_port_to_docker(mgmt_bridge, ptf_mgmt_ip_addr, ptf_mgmt_ip_gw,
                                        ptf_mgmt_ipv6_addr, ptf_mgmt_ipv6_gw, ptf_extra_mgmt_ip_addr)

            ptf_bp_ip_addr = module.params['ptf_bp_ip_addr']
            ptf_bp_ipv6_addr = module.params['ptf_bp_ipv6_addr']

            if net.netns:
                net.unbind_mgmt_port(NETNS_MGMT_IF_TEMPLATE % net.vm_set_name)
                net.delete_network_namespace()

            if vms_exists:
                net.unbind_fp_ports(batch_mode=batch_mode)
                if is_vs_chassis:
                    net.unbind_vs_chassis_ports(duts_midplane_ports, duts_inband_ports)
                net.add_injected_fp_ports_to_docker()
                net.add_injected_VM_ports_to_docker()
                net.bind_fp_ports(batch_mode=batch_mode)
                net.bind_vm_backplane()
                net.add_bp_port_to_docker(ptf_bp_ip_addr, ptf_bp_ipv6_addr)
                if is_vs_chassis:
                    net.bind_vs_chassis_ports(duts_midplane_ports, duts_inband_ports)

            if net.netns:
                net.add_network_namespace()
                # Let's enable arp_filter in the netns
                # to prevent arp flux
                net.enable_arp_filter_netns()
                net.add_mgmt_port_to_netns(
                    mgmt_bridge, netns_mgmt_ip_addr, ptf_mgmt_ip_gw)
                net.enable_netns_loopback()

            if hostif_exists:
                net.add_host_ports()

            if net.netns:
                net.setup_netns_source_routing()

            if devices_interconnect_exists:
                net.bind_devices_interconnect()

        elif cmd == 'connect-vms' or cmd == 'disconnect-vms':
            check_params(module, ['vm_set_name',
                                  'topo',
                                  'duts_fp_ports'], cmd)

            vm_set_name = module.params['vm_set_name']
            topo = module.params['topo']
            duts_fp_ports = module.params['duts_fp_ports']
            duts_name = module.params['duts_name']
            is_multi_duts = True if len(duts_name) > 1 else False

            if len(vm_set_name) > VM_SET_NAME_MAX_LEN:
                raise Exception("vm_set_name can't be longer than %d characters: %s (%d)" % (
                    VM_SET_NAME_MAX_LEN, vm_set_name, len(vm_set_name)))

            hostif_exists, vms_exists = check_topo(topo, is_multi_duts)

            if vms_exists:
                check_params(module, ['vm_base'], cmd)
                vm_base = module.params['vm_base']
            else:
                vm_base = None

            net.init(vm_set_name, vm_base, duts_fp_ports, duts_name)

            if vms_exists:
                if cmd == 'connect-vms':
                    net.bind_fp_ports()
                else:
                    net.bind_fp_ports(True)
        else:
            raise Exception("Got wrong cmd: %s. Ansible bug?" % cmd)

    except Exception as error:
        logging.error(traceback.format_exc())
        module.fail_json(msg=str(error))

    module.exit_json(changed=True)


if __name__ == "__main__":
    main()
