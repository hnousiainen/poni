import os
import copy
import time
import logging
from . import cloudbase

class VSphereProvider(cloudbase.Provider):
    def __init__(self, cloud_prop):
        self.log = logging.getLogger('poni.vsphere')
        self.vi_url = os.environ.get('VI_URL') or cloud_prop.get("vi_url")
        assert self.vi_url, "either the enviroment variable VI_URL or vcenter_url property must be set for vSphere instances"
        self.vi_username = os.environ.get('VI_USERNAME') or cloud_prop.get("vi_username")
        assert self.vi_username, "either the enviroment variable VI_USERNAME or vi_username property must be set for vSphere instances"
        self.vi_password = os.environ.get('VI_PASSWORD') or cloud_prop.get("vi_password")
        assert self.vi_password, "either the enviroment variable VI_PASSWORD or vi_password property must be set for vSphere instances"
        try:
            from pyvsphere.vim25 import Vim
        except ImportError:
            assert 0, "pyvsphere must be installed for vSphere instances to work"
        self.vim = Vim(self.vi_url)
        self.vim.login(self.vi_username, self.vi_password)
        self.instances = {}
        self._base_vm_cache = {}

    @classmethod
    def get_provider_key(cls, cloud_prop):
        """
        Return a cloud Provider object for the given cloud properties.
        """
        return "vSphere"

    def _get_instance(self, prop):
        """
        Get a VM instance either a cache or directly from vSphere and
        establish the current state of the VM.
        """
        vm_name = instance_id = prop.get('vm_name', None)
        assert vm_name, "vm_name must be specified for vSphere instances"
        base_vm_name = prop.get('base_vm_name', None)
        assert base_vm_name, "base_vm_name must be specified for vSphere instances"
        instance = self.instances.get(instance_id)
        if not instance:
            vm_state = 'VM_NON_EXISTENT'
            vm = self.vim.find_vm_by_name(vm_name, ['summary', 'snapshot'])
            if vm:
                self.log.debug('VM %s already exists', vm_name)
                vm_state = 'VM_DIRTY'
                if (hasattr(vm, 'snapshot') and
                    vm.snapshot.rootSnapshotList and
                    vm.snapshot.rootSnapshotList[0].name == 'pristine'):
                    vm_state = 'VM_CLEAN'
                    if vm.power_state() == 'poweredOn':
                        vm_state = 'VM_RUNNING'
            self.log.debug("Instance %s is in %s state", vm_name, vm_state)
            instance = dict(id=instance_id, vm=vm, vm_name=vm_name,
                            base_vm_name=base_vm_name, vm_state=vm_state)
            self.instances[instance_id] = instance
        return instance

    def init_instance(self, cloud_prop):
        """
        Create a new instance with the given properties.

        Returns node properties that are changed.
        """
        instance = self._get_instance(cloud_prop)
        # Establish the current state of VM
        out_prop = copy.deepcopy(cloud_prop)
        # Limit the state to VM_CLEAN at best so init will have to revert the snapshot, at least
        instance["vm_state"] = 'VM_CLEAN' if instance['vm_state'] == 'VM_RUNNING' else instance['vm_state']
        out_prop["instance"] = instance['id']
        return dict(cloud=out_prop)

    def get_instance_status(self, prop):
        """
        Return instance status string for the instance specified in the given
        cloud properties dict.
        """
        assert 0, "implement in sub-class"

    def terminate_instances(self, props):
        """
        Terminate instances specified in the given sequence of cloud
        properties dicts.
        """
        jobs = {}
        tasks = {}
        for prop in props:
            instance_id = prop['instance']
            instance = self._get_instance(prop)
            assert instance, "instance %s not found. Very bad. Should not happen. Ever." % instance_id
            vm_state = instance['vm_state']
            if vm_state != 'VM_NON_EXISTENT':
                jobs[instance_id] = self._delete_vm(instance)
                tasks[instance_id] = None
        while jobs:
            if [tasks[x] for x in tasks if tasks[x]]:
                _,tasks = self.vim.update_many_objects(tasks)
            for instance_id in list(jobs):
                try:
                    job = jobs[instance_id]
                    # This is where the magic happens: the generator is fed the
                    # latest updated Task and returns the same or the next one
                    # to poll.
                    tasks[instance_id] = job.send(tasks[instance_id])
                except StopIteration:
                    del tasks[instance_id]
                    del jobs[instance_id]
            self.log.info("[%s/%s] instances being terminated, waiting...", len(props)-len(jobs), len(props))
            time.sleep(2)

    def wait_instances(self, props, wait_state="running"):
        """
        Wait for all the given instances to reach status specified by
        the 'wait_state' argument.

        Returns a dict {instance_id: dict(<updated properties>)}

        @note: This function uses generators to simulate co-operative,
               non-blocking multitasking. The generators generate and
               get fed a sequence of Task status objects. Once the
               generator is done, it will simply exit.
               All this complexity is necessary to work around the
               problem that each of the jobs might takes ages to finish,
               hence doing them in a sequential order is highly undesirable.
        """
        jobs = {}
        tasks = {}
        for prop in props:
            instance_id = prop['instance']
            instance = self._get_instance(prop)
            assert instance, "instance %s not found. Very bad. Should not happen. Ever." % instance_id
            vm_state = instance['vm_state']
            job = None
            if wait_state == 'running':
                # Get the VM running from whatever state it's in
                if vm_state == 'VM_CLEAN':
                    job = self._revert_vm(instance)
                elif vm_state == 'VM_NON_EXISTENT':
                    job = self._clone_vm(instance, nuke_old=True)
                elif vm_state == 'VM_DIRTY':
                    job = self._clone_vm(instance, nuke_old=True)
            else:
                # Handle the update
                if vm_state == 'VM_RUNNING':
                    job = self._update_vm(instance)
            if job:
                jobs[instance_id] = job
                tasks[instance_id] = None
        # Keep running the jobs until they are all done
        updated_props = {}
        while jobs:
            if [tasks[x] for x in tasks if tasks[x]]:
                _,tasks = self.vim.update_many_objects(tasks)
            for instance_id in list(jobs):
                try:
                    job = jobs[instance_id]
                    # This is where the magic happens: the generator is fed the
                    # latest updated Task and returns the same or the next one
                    # to poll.
                    tasks[instance_id] = job.send(tasks[instance_id])
                except StopIteration:
                    self.log.debug("%s entered state: %s", instance_id, wait_state)
                    del tasks[instance_id]
                    del jobs[instance_id]
                    self.instances[instance_id]['vm_state'] = 'VM_RUNNING'
                    # Collect the IP address which should be in there by now
                    ipv4=self.instances[instance_id]['ipv4']
                    private=dict(ip=ipv4,
                                 dns=ipv4)
                    updated_props[instance_id] = dict(host=ipv4, private=private)
            # self.log.info("[%s/%s] instances %r, waiting...", len(updated_props), len(props), wait_state)
            time.sleep(2)

        return updated_props

    def _get_base_vm(self, base_vm_name):
        """
        Get a VM object for the base image for cloning with a bit of caching

        @param base_vm_name: name of the VM to find

        @returns: VM object or None if not found
        """
        base_vm = self._base_vm_cache.get(base_vm_name, None)
        if not base_vm:
            base_vm = self.vim.find_vm_by_name(base_vm_name)
            if base_vm:
                self._base_vm_cache[base_vm_name] = base_vm
        return base_vm

    def _clone_vm(self, instance, nuke_old=False):
        """
        Perform a full clone-poweron-snapshot cycle on the instance

        This is a generator function which is used in a co-operative
        multitasking manner. See wait_instances() for an idea on its
        usage.

        @param instance: dict of the VM instance to create
        @param nuke_old: should an existing VM with the same be nuked

        @return: generator function
        """
        def done(task):
            return (hasattr(task, 'info') and
                    (task.info.state == 'success' or
                     task.info.state == 'error'))

        def got_ip(task):
            return (hasattr(task, 'summary') and
                    getattr(task.summary.guest, 'ipAddress', None))

        vm_name = instance['vm_name']
        base_vm = self._get_base_vm(instance['base_vm_name'])
        assert base_vm, "base VM %s not found, check the cloud.base_vm_name property for %s" % (instance['base_vm_name'], vm_name)

        if nuke_old:
            clone = self.vim.find_vm_by_name(vm_name)
            if clone:
                self.log.debug("CLONE(%s) POWEROFF STARTING" % vm_name)
                task = clone.power_off_task()
                while not done(task):
                    task = (yield task)
                self.log.debug("CLONE(%s) POWEOFF DONE" % vm_name)
                self.log.debug("CLONE(%s) DELETE STARTING" % vm_name)
                task = clone.delete_vm_task()
                while not done(task):
                    task = (yield task)
                self.log.debug("CLONE(%s) DELETE DONE" % vm_name)

        self.log.debug("CLONE(%s) CLONE STARTING" % vm_name)
        task = base_vm.clone_vm_task(vm_name, linked_clone=False)
        while not done(task):
            task = (yield task)
        self.log.debug("CLONE(%s) CLONE DONE" % vm_name)

        clone = self.vim.find_vm_by_name(vm_name)

        self.log.debug("CLONE(%s) POWERON STARTING" % vm_name)
        task = clone.power_on_task()
        while not done(task):
            task = (yield task)
        self.log.debug("CLONE(%s) POWERON DONE" % vm_name)

        self.log.debug("CLONE(%s) WAITING FOR IP" % (vm_name))
        task = clone
        while not got_ip(task):
            task = (yield task)
        self.log.debug("CLONE(%s) GOT IP: %s" % (vm_name, task.summary.guest.ipAddress))
        instance['ipv4'] = task.summary.guest.ipAddress

        self.log.debug("CLONE(%s) SNAPSHOT STARTING" % vm_name)
        task = clone.create_snapshot_task('pristine', memory=True)
        while not done(task):
            task = (yield task)
        self.log.debug("CLONE(%s) SNAPSHOT DONE" % vm_name)

    def _revert_vm(self, instance):
        """
        Perform a quick snapshot revert on a VM instance

        This is a generator function which is used in a co-operative
        multitasking manner. See wait_instances() for an idea on its
        usage.

        @param instance: dict of the VM instance to create

        @return: generator function
        """
        def done(task):
            return (hasattr(task, 'info') and
                    (task.info.state == 'success' or
                     task.info.state == 'error'))

        def got_ip(task):
            return (hasattr(task, 'summary') and
                    getattr(task.summary.guest, 'ipAddress', None))

        vm_name = instance['vm_name']
        vm = instance['vm']
        if not vm:
            vm = self.vim.find_vm_by_name(vm_name)
        assert vm, "VM %s not found in vSphere, something is terribly wrong here" % vm_name

        self.log.debug("REVERT(%s) STARTING" % vm_name)
        task = vm.revert_to_current_snapshot_task()
        while not done(task):
            task = (yield task)
        self.log.debug("REVERT(%s) DONE" % vm_name)

        self.log.debug("REVERT(%s) WAITING FOR IP" % (vm_name))
        task = vm
        while not got_ip(task):
            task = (yield task)
        self.log.debug("REVERT(%s) GOT IP: %s" % (vm_name, task.summary.guest.ipAddress))
        instance['ipv4'] = task.summary.guest.ipAddress

    def _delete_vm(self, instance):
        """
        Power off and delete a VM

        This is a generator function which is used in a co-operative
        multitasking manner. See wait_instances() for an idea on its
        usage.

        @param instance: dict of the VM instance to delete

        @return: generator function
        """
        def done(task):
            return (hasattr(task, 'info') and
                    (task.info.state == 'success' or
                     task.info.state == 'error'))

        vm_name = instance['vm_name']
        vm = instance['vm']
        if not vm:
            vm = self.vim.find_vm_by_name(vm_name, ['summary'])
        assert vm, "VM %s not found in vSphere, something is terribly wrong here" % vm_name

        if vm.power_state() == 'poweredOn':
            self.log.debug("DELETE(%s) POWEROFF STARTING" % vm_name)
            task = vm.power_off_task()
            while not done(task):
                task = (yield task)
            self.log.debug("DELETE(%s) POWEROFF DONE" % vm_name)

        self.log.debug("DELETE(%s) DELETE STARTING" % vm_name)
        task = vm.delete_vm_task()
        while not done(task):
            task = (yield task)
        self.log.debug("DELETE(%s) DELETE DONE" % vm_name)

    def _update_vm(self, instance):
        """
        Get updated info from the VM instance

        This is a generator function which is used in a co-operative
        multitasking manner. See wait_instances() for an idea on its
        usage.

        @param instance: dict of the VM instance to update

        @return: generator function
        """
        def got_ip(task):
            return (hasattr(task, 'summary') and
                    getattr(task.summary.guest, 'ipAddress', None))

        vm_name = instance['vm_name']
        vm = instance['vm']
        if not vm:
            vm = self.vim.find_vm_by_name(vm_name)
        assert vm, "VM %s not found in vSphere, something is terribly wrong here" % vm_name

        self.log.debug("UPDATE(%s) WAITING FOR IP" % (vm_name))
        task = vm
        while not got_ip(task):
            task = (yield task)
        self.log.debug("UPDATE(%s) GOT IP: %s" % (vm_name, task.summary.guest.ipAddress))
        instance['ipv4'] = task.summary.guest.ipAddress
