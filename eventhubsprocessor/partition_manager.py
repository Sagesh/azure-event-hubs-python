"""
Author: Aaron (Ari) Bornstien
"""
from collections import OrderedDict, Counter
import requests, time
from bs4 import BeautifulSoup
from eventhubsprocessor.eh_partition_pump import EventHubPartitionPump
from eventhubsprocessor.cancellation_token import CancellationToken


class PartitionManager:
    """
    Manages the partition event pump execution
    """
    def __init__(self, host):
        self.host = host
        self.partition_pumps = {}
        self.partition_ids = None
        self.run_task = None
        self.cancellation_token = CancellationToken() 

    async def get_partition_ids_async(self):
        """
        Returns a list of all the event hub partition ids
        """
        if not self.partition_ids:
            try:
                headers = {"Content-Type":"application/json;type=entry;charset=utf-8",
                           "Authorization": self.host.eh_rest_auth["token"],
                           "Host":"k8sworkshop.servicebus.windows.net"
                          }

                res = requests.get('https://{}.servicebus.windows.net/{}?timeout=60&api-version=2014-01'.format(self.host.eh_rest_auth["sb_name"],
                                                                                                                self.host.eh_rest_auth["eh_name"]),
                                                                                                                headers=headers)
                soup = BeautifulSoup(res.text, "lxml-xml") # process xml response
                self.partition_ids = [pid.text for pid in soup.find("PartitionIds")]
            except Exception as err:
                raise Exception("failed to get partition ids", err)

        print(self.host.guid, "PartitionCount: {}".format(len(self.partition_ids)))
        return self.partition_ids

    async def start_async(self):
        """
        Intializes the partition checkpoint and lease store and then calls run async.
        """
        if self.run_task:
            raise Exception("A PartitionManager cannot be started multiple times.")

        await self.initialize_stores_async()
        self.run_task = await self.run_async()

    async def stop_async(self):
        """
        Terminiates the partition manger.
        """
        self.cancellation_token.cancel()
        local_run_task = self.run_task
        if local_run_task:
            await local_run_task()

    async def run_async(self):
        """
        Starts the run loop and manages exceptions and cleanup
        """
        try:
            await self.run_loop_async(self.cancellation_token)
        except Exception as err:
            print("run loop failed ", err) # TBI add unified error handling

        try:
            print(self.host.guid, "Shutting down all pumps")
            await self.remove_all_pumps_async("Shutdown")
        except Exception as err:
            raise Exception("failed to remove all pumps", err) # TBI add unified error handling

    async def initialize_stores_async(self):
        """
        Intializes the partition checkpoint and lease store ensures that a checkpoint
        exists for all partitions. Note in this case checkpoint and lease stores are
        the same storage manager construct. TBD (Add retries)
        """
        await self.host.storage_manager.create_checkpoint_store_if_not_exists_async()
        partition_ids = await self.get_partition_ids_async()
        for p_id in partition_ids:
            await self.retry_async(self.host.storage_manager.create_checkpoint_if_not_exists_async,
                                   p_id, "Failure creating checkpoint for partition, retrying",
                                   "Out of retries creating checkpoint blob for partition", 5)

    async def retry_async(self, func, partition_id, retry_message,
                          final_failure_message, max_retries):
        """
        Throws if it runs out of retries. If it returns, action succeeded
        """
        created_okay = False
        retry_count = 0
        while not created_okay and retry_count <= max_retries:
            try:
                await func(partition_id)
                created_okay = True
            except Exception as err:
                print(self.host.guid, partition_id, retry_message, err)
                retry_count += 1
        if not created_okay:
            raise Exception(self.host.guid, final_failure_message)

    async def run_loop_async(self, cancellation_token):
        """
        This is the main execution loop for allocating and manging pumps
        """
        while not cancellation_token.is_cancelled:
            lease_manager = self.host.storage_manager
            all_leases = {}
            # Inspect all leases.
            # Acquire any expired leases.
            # Renew any leases that currently belong to us.
            getting_all_leases = await lease_manager.get_all_leases()
            leases_owned_by_others = []
            our_lease_count = 0

            for get_lease_task in getting_all_leases:
                try:
                    possible_lease = await get_lease_task
                    all_leases[possible_lease.partition_id] = possible_lease
                    if possible_lease.is_expired():
                        print("Trying to aquire lease", self.host.guid, possible_lease.partition_id)
                        if await lease_manager.acquire_lease_async(possible_lease):
                            our_lease_count += 1
                        else:
                            leases_owned_by_others.append(possible_lease)

                    elif possible_lease.owner == self.host.host_name:
                        try:
                            print("Trying to renew lease.", self.host.guid,
                                  possible_lease.partition_id)
                            await lease_manager.renew_lease_async(possible_lease)
                            our_lease_count += 1
                        except Exception as err: #Update to LeaseLostException:
                            print("Lease lost exception", err, self.host.guid, possible_lease.partition_id)
                            leases_owned_by_others.append(possible_lease)
                    else:
                        leases_owned_by_others.append(possible_lease)

                except Exception as err:
                    raise Exception("Error checking leases", err) #Unified error handling TBI

            # Grab more leases if available and needed for load balancing
            leases_owned_by_others_count = len(leases_owned_by_others)
            if leases_owned_by_others_count > 0:
                steal_this_lease = self.which_lease_to_steal(leases_owned_by_others, our_lease_count)
                if steal_this_lease:
                    try:
                        #there is a bug here (find out why i can't steal the lease)
                        print("Lease to steal", steal_this_lease.serializable())
                        if await lease_manager.acquire_lease_async(steal_this_lease):
                            print("Stole lease sucessfully", self.host.guid, steal_this_lease.partition_id)
                        else:
                            print("Failed to steal lease for partition ", self.host.guid, steal_this_lease.partition_id)
                    except Exception as err:
                        print("Failed to steal lease", err) #Unified error handling TBI

            for partition_id in all_leases:
                try:
                    updated_lease = all_leases[partition_id]
                    if updated_lease.owner == self.host.host_name:
                        await self.check_and_add_pump_async(partition_id, updated_lease)
                    else:
                        await self.remove_pump_async(partition_id, "LeaseLost")
                except Exception as err:
                    print("failed to update lease", err) #Unified error handling TBI

                # time.sleep(lease_manager.lease_renew_interval)

    async def check_and_add_pump_async(self, partition_id, lease):
        """
        Updates the lease on an exisiting pump
        """
        captured_pump = self.partition_pumps.get(partition_id)
        if captured_pump:
            # There already is a pump. Make sure the pump is working and replace the lease.
            if captured_pump.pump_status == "Errored" or captured_pump.pump_status == "IsClosing":
                # The existing pump is bad. Remove it.
                await self.remove_pump_async(partition_id, "Shutdown")
            else: #Pump is working, just replace the lease.
                captured_pump.set_lease(lease)
        else:
            await self.create_new_pump_async(partition_id, lease)

    async def create_new_pump_async(self, partition_id, lease):
        """
        Create a new pump thread with a given lease
        """
        partition_pump = EventHubPartitionPump(self.host, lease)
        # Do the put after start, if the start fails then put doesn't happen
        await partition_pump.open_async()
        self.partition_pumps[partition_id] = partition_pump
        print("Created new partition pump ", self.host.guid, partition_id)

    async def remove_pump_async(self, partition_id, reason):
        """
        Stops a single partiton pump
        """
        captured_pump = self.partition_pumps.get(partition_id)
        if captured_pump:
            if not captured_pump.is_closing():
                await captured_pump.close_async(reason)
            #else, pump is already closing/closed, don't need to try to shut it down again
        else:
            # PartitionManager main loop tries to remove pump for every partition that the
            # host does not own, just to be sure. Not finding a pump for a partition is normal
            # and expected most of the time.
            print(self.host.guid, partition_id, "No pump found to remove for this partition")

    async def remove_all_pumps_async(self, reason):
        """
        Stops all partition pumps 
        (Note this might be wrong and need to await all tasks before returning done)
        """
        for p_id in self.partition_pumps:
            await self.remove_pump_async(p_id, reason)
        return True

    def which_lease_to_steal(self, stealable_leases, have_lease_count):
        """
        Determines and return which lease to steal
        If the number of leases is a multiple of the number of hosts, then the desired
        configuration is that all hosts own the name number of leases, and the
        difference between the "biggest" owner and any other is 0.

        If the number of leases is not a multiple of the number of hosts, then the most
        even configurationpossible is for some hosts to have (self, leases/hosts) leases
        and others to have (self, (self, leases/hosts) + 1). For example, for 16 partitions
        distributed over five hosts, the distribution would be 4, 3, 3, 3, 3, or any of the
        possible reorderings.

        In either case, if the difference between this host and the biggest owner is 2 or more,
        then thesystem is not in the most evenly-distributed configuration, so steal one lease
        from the biggest. If there is a tie for biggest, we pick whichever appears first in the
        list because it doesn't really matter which "biggest" is trimmed down.

        Stealing one at a time prevents flapping because it reduces the difference between the
        biggest and this host by two at a time. If the starting difference is two or greater,
        then the difference cannot end up below 0. This host may become tied for biggest, but it
        cannot become larger than the host that it is stealing from.
        """
        counts_by_owner = self.count_leases_by_owner(stealable_leases)
        biggest_owner = (sorted(counts_by_owner.items(), key=lambda kv:kv[1])).pop()
        steal_this_lease = None
        if (biggest_owner[1] - have_lease_count) >= 2:
            steal_this_lease = [l for l in stealable_leases if l.owner == biggest_owner[0]][0]

        return steal_this_lease

    def count_leases_by_owner(self, leases):
        """
        Returns a dictionary of leases by current owner
        """
        owners = [l.owner for l in leases]
        return dict(Counter(owners))
