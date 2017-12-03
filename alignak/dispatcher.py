# -*- coding: utf-8 -*-
#
# Copyright (C) 2015-2016: Alignak team, see AUTHORS.txt file for contributors
#
# This file is part of Alignak.
#
# Alignak is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Alignak is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with Alignak.  If not, see <http://www.gnu.org/licenses/>.
#
#
# This file incorporates work covered by the following copyright and
# permission notice:
#
#  Copyright (C) 2009-2014:
#     xkilian, fmikus@acktomic.com
#     Guillaume Bour, guillaume@bour.cc
#     aviau, alexandre.viau@savoirfairelinux.com
#     Httqm, fournet.matthieu@gmail.com
#     Hartmut Goebel, h.goebel@goebel-consult.de
#     Nicolas Dupeux, nicolas@dupeux.net
#     Grégory Starck, g.starck@gmail.com
#     Sebastien Coavoux, s.coavoux@free.fr
#     Christophe Simon, geektophe@gmail.com
#     Jean Gabes, naparuba@gmail.com
#     Zoran Zaric, zz@zoranzaric.de

#  This file is part of Shinken.
#
#  Shinken is free software: you can redistribute it and/or modify
#  it under the terms of the GNU Affero General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  Shinken is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU Affero General Public License for more details.
#
#  You should have received a copy of the GNU Affero General Public License
#  along with Shinken.  If not, see <http://www.gnu.org/licenses/>.

"""
 This is the class of the dispatcher. Its role is to dispatch
 configurations to other elements like schedulers, reactionner,
 pollers, receivers and brokers. It is responsible for high availability part. If an
 element dies and the element type has a spare, it sends the config of the
 dead one to the spare
"""

import sys
import cPickle
import logging
import time
import random

from alignak.util import alive_then_spare_then_deads

logger = logging.getLogger(__name__)  # pylint: disable=C0103

# Always initialize random :)
random.seed()


class Dispatcher:
    """Dispatcher is in charge of sending configuration to other daemon.
    It has to handle spare, realms, poller tags etc.
    """

    def __init__(self, conf, arbiter_link):
        """Initialize the dispatcher

        Note that the arbiter param is an ArbiterLink, not an Arbiter daemon. Thus it is only
        an interface to the running Arbiter daemon...

        :param conf: the whole Alignak configuration as parsed by the Arbiter
        :type conf: Config
        :param arbiter_link: the link to the arbiter that parsed this configuration
        :type arbiter_link: ArbiterLink
        """
        self.arbiter_link = arbiter_link
        self.conf = conf

        logger.debug("Dispatcher __init__: %s / %s", self.arbiter_link, self.conf)
        if hasattr(self.conf, 'confs'):
            logger.debug("Dispatch conf confs: %s", self.conf.confs)
        else:
            logger.debug("Dispatch conf has no confs")

        logger.debug("Dispatcher configuration packs:")
        print("Dispatcher configuration packs:")
        for cfg_id in self.conf.packs:
            pack = self.conf.packs[cfg_id]
            print("  . %s, flavor:%s, %s" %
                  (pack.uuid,
                   getattr(pack, 'push_flavor', 'None'),
                   pack))

        # Direct pointer to important elements for us
        self.realms = conf.realms
        logger.debug("Dispatcher realms configuration:")
        print("Dispatcher realms configuration:")
        for realm in self.realms:
            logger.debug("- %s", realm)
            print("- %s" % realm)
            for cfg_id in realm.confs:
                realm_config = realm.confs[cfg_id]
                print("  . %s, flavor:%s, %s" %
                      (realm_config.uuid,
                       getattr(realm_config, 'push_flavor', 'None'),
                       realm_config))

        logger.debug("Dispatcher satellites configuration:")
        print("Dispatcher satellites configuration:")
        for sat_type in ('arbiters', 'schedulers', 'reactionners',
                         'brokers', 'receivers', 'pollers'):
            setattr(self, sat_type, getattr(self.conf, sat_type))
            logger.debug("- %s", getattr(self, sat_type))
            print("- %s" % getattr(self, sat_type))

            # for each satellite, we look if current arbiter have a specific
            # satellitemap value set for this satellite.
            # if so, we give this map to the satellite (used to build satellite URI later)
            if arbiter_link is None:
                continue

            for satellite in getattr(self, sat_type):
                satellite.set_arbiter_satellitemap(arbiter_link.satellitemap.get(satellite.name, {}))

        self.dispatch_queue = {'schedulers': [], 'reactionners': [],
                               'brokers': [], 'pollers': [], 'receivers': []}

        # I remove this because it is already done when the packs are built!
        # for cfg in self.conf.confs.values():
        #     cfg.is_assigned = False
        #     cfg.assigned_to = None
        #     cfg.push_flavor = 0

        self.elements = []  # all elements, including schedulers and satellites
        self.satellites = []  # only satellites not schedulers

        # Add satellites in the good lists
        self.elements.extend(self.schedulers)

        # Others are in 2 lists
        self.elements.extend(self.reactionners)
        self.satellites.extend(self.reactionners)
        self.elements.extend(self.pollers)
        self.satellites.extend(self.pollers)
        self.elements.extend(self.brokers)
        self.satellites.extend(self.brokers)
        self.elements.extend(self.receivers)
        self.satellites.extend(self.receivers)

        # Some flag about dispatch need or not
        self.dispatch_ok = False
        self.first_dispatch_done = False

        # Prepare the satellites confs
        print("Prepare satellites configuration:")
        for satellite in self.satellites:
            satellite.prepare_for_conf()
            print("- %s: %s" % (satellite.name, satellite.cfg))

        # Some properties must be given to satellites from global
        # todo: This should not be necessary ! The pollers should have their own configuration!
        # todo: indeed, this should be done for all the global alignak configuration parameters!!!
        # configuration, like the max_plugins_output_length to pollers
        parameters = {'max_plugins_output_length': self.conf.max_plugins_output_length}
        for poller in self.pollers:
            poller.add_global_conf_parameters(parameters)

        # Reset need_conf for all schedulers.
        for sched in self.schedulers:
            sched.need_conf = True
        # Same for receivers
        for rec in self.receivers:
            rec.need_conf = True

    def check_alive(self):
        """Check all daemons state (alive or not)
        and send conf if necessary

        :return: None
        """
        now = time.time()
        for elt in self.elements:
            elt.update_infos(now)

            # Not alive needs new need_conf
            # and spare too if they do not have already a conf
            # REF: doc/alignak-scheduler-lost.png (1)
            if not elt.alive or hasattr(elt, 'conf') and elt.conf is None:
                elt.need_conf = True

        for arb in self.arbiters:
            # If not me, but not the master too
            if arb != self.arbiter_link and arb.spare:
                arb.update_infos(now)

    def check_dispatch(self):
        """Check if all active items are still alive

        :return: None
        """
        # Check if the other arbiter has a conf, but only if I am a master
        for arb in self.arbiters:
            # If not me and I'm a master
            if arb != self.arbiter_link and self.arbiter_link and not self.arbiter_link.spare:
                if not arb.have_conf(self.conf.magic_hash):
                    if not hasattr(self.conf, 'whole_conf_pack'):
                        logger.error('CRITICAL: the arbiter try to send a configuration but '
                                     'it is not a MASTER one?? Look at your configuration.')
                        continue
                    logger.info('Configuration sent to arbiter: %s', arb.get_name())
                    arb.put_conf(self.conf.whole_conf_pack)
                    # Remind it that WE are the master here!
                    arb.do_not_run()
                else:
                    # Ok, it already has the conf. I remember that
                    # it does not have to run, I'm still alive!
                    arb.do_not_run()

        # We check for confs to be dispatched on alive schedulers. If not dispatched, need
        # dispatch :) and if dispatch on a failed node, remove the association, and need a new
        # dispatch
        for realm in self.realms:
            for cfg_id in realm.confs:
                conf_uuid = realm.confs[cfg_id].uuid
                push_flavor = realm.confs[cfg_id].push_flavor
                sched = realm.confs[cfg_id].assigned_to
                if sched is None:
                    if self.first_dispatch_done:
                        logger.info("Scheduler configuration %s is unmanaged!!", conf_uuid)
                    self.dispatch_ok = False
                else:
                    logger.debug("Realm %s - Checking Scheduler %s configuration: %s",
                                 realm.name, sched.scheduler_name, conf_uuid)
                    if not sched.alive:
                        self.dispatch_ok = False  # so we ask a new dispatching
                        logger.warning("Scheduler %s has the configuration '%s' but it is dead, "
                                       "I am not happy.", sched.get_name(), conf_uuid)
                        sched.conf.assigned_to = None
                        sched.conf.is_assigned = False
                        sched.conf.push_flavor = 0
                        sched.push_flavor = 0
                        sched.conf = None
                    # Maybe the scheduler restarts, so is alive but without
                    # the conf we think it was managing so ask it what it is
                    # really managing, and if not, put the conf unassigned
                    if not sched.do_i_manage(conf_uuid, push_flavor):
                        self.dispatch_ok = False  # so we ask a new dispatching
                        logger.warning("Scheduler '%s' do not manage this configuration: %s, "
                                       "I am not happy.", sched.get_name(), conf_uuid)
                        if sched.conf:
                            sched.conf.assigned_to = None
                            sched.conf.is_assigned = False
                            sched.conf.push_flavor = 0
                        sched.push_flavor = 0
                        sched.need_conf = True
                        sched.conf = None

        self.check_dispatch_other_satellites()

    def check_dispatch_other_satellites(self):
        """
        Check the dispatch in other satellites: reactionner, poller, broker, receiver

        :return: None
        """
        # Maybe satellites are alive, but do not have a cfg yet.
        # I think so. It is not good. I ask a global redispatch for
        # the cfg_id I think is not correctly dispatched.
        for realm in self.realms:
            # Todo: Spare arbiter fails else...
            if not hasattr(realm, 'confs'):
                continue
            for cfg_id in realm.confs:
                conf_uuid = realm.confs[cfg_id].uuid
                push_flavor = realm.confs[cfg_id].push_flavor
                try:
                    for sat_type in ('reactionner', 'poller', 'broker', 'receiver'):
                        # We must have the good number of satellite or we are not happy
                        # So we are sure to raise a dispatch every loop a satellite is missing
                        if (len(realm.to_satellites_managed_by[sat_type][conf_uuid]) <
                                realm.get_nb_of_must_have_satellites(sat_type)):
                            logger.warning("Missing satellite %s for configuration %s:",
                                           sat_type, conf_uuid)

                            # TODO: less violent! Must only resent to who need?
                            # must be caught by satellite who sees that
                            # it already has the conf and do nothing
                            self.dispatch_ok = False  # so we will redispatch all
                            realm.to_satellites_need_dispatch[sat_type][conf_uuid] = True
                            realm.to_satellites_managed_by[sat_type][conf_uuid] = []
                        for satellite in realm.to_satellites_managed_by[sat_type][conf_uuid]:
                            # Maybe the sat was marked as not alive, but still in
                            # to_satellites_managed_by. That means that a new dispatch
                            # is needed
                            # Or maybe it is alive but I thought that this reactionner
                            # managed the conf and it doesn't.
                            # I ask a full redispatch of these cfg for both cases

                            if push_flavor == 0 and satellite.alive:
                                logger.warning('[%s] The %s %s manage a unmanaged configuration',
                                               realm.get_name(), sat_type, satellite.get_name())
                                continue
                            if satellite.alive and (not satellite.reachable or
                                                    satellite.do_i_manage(conf_uuid, push_flavor)):
                                continue

                            logger.warning('[%s] The %s %s seems to be down, '
                                           'I must re-dispatch its role to someone else.',
                                           realm.get_name(), sat_type, satellite.get_name())
                            self.dispatch_ok = False  # so we will redispatch all
                            realm.to_satellites_need_dispatch[sat_type][conf_uuid] = True
                            realm.to_satellites_managed_by[sat_type][conf_uuid] = []
                # At the first pass, there is no conf_id in to_satellites_managed_by
                except KeyError:
                    pass

    def check_bad_dispatch(self):
        """Check if we have a bad dispatch
        For example : a spare started but the master was still alive
        We need ask the spare to wait a new conf

        :return: None
        """
        for elt in self.elements:
            if hasattr(elt, 'conf'):
                # If element has a conf, I do not care, it's a good dispatch
                # If dead: I do not ask it something, it won't respond..
                if elt.conf is None and elt.reachable:
                    if elt.have_conf():
                        logger.warning("The element %s have a conf and should "
                                       "not have one! I ask it to idle now",
                                       elt.get_name())
                        elt.active = False
                        elt.wait_new_conf()
                        # I do not care about order not send or not. If not,
                        # The next loop will resent it

        # I ask satellites which sched_id they manage. If I do not agree, I ask
        # them to remove it
        for satellite in self.satellites:
            sat_type = satellite.type
            if not satellite.reachable:
                continue
            cfg_ids = satellite.managed_confs  # what_i_managed()
            # I do not care about satellites that do nothing, they already
            # do what I want :)
            if not cfg_ids:
                continue
            id_to_delete = []
            for cfg_id in cfg_ids:
                # Ok, we search for realms that have the conf
                for realm in self.realms:
                    if cfg_id in realm.confs:
                        conf_uuid = realm.confs[cfg_id].uuid
                        # Ok we've got the realm, we check its to_satellites_managed_by
                        # to see if reactionner is in. If not, we remove he sched_id for it
                        if satellite not in realm.to_satellites_managed_by[sat_type][conf_uuid]:
                            id_to_delete.append(cfg_id)
            # Maybe we removed all conf_id of this reactionner
            # We can put it idle, no active and wait_new_conf
            if len(id_to_delete) == len(cfg_ids):
                satellite.active = False
                logger.info("I ask %s to wait for a new conf", satellite.get_name())
                satellite.wait_new_conf()
            else:
                # It is not fully idle, just less cfg
                for r_id in id_to_delete:
                    logger.info("I ask %s to remove configuration %d",
                                satellite.get_name(), r_id)
                    satellite.remove_from_conf(id)

    def get_scheduler_ordered_list(self, realm):
        """Get sorted scheduler list for a specific realm

        :param realm: realm we want scheduler from
        :type realm: object
        :return: sorted scheduler list
        :rtype: list[alignak.objects.schedulerlink.SchedulerLink]
        """
        # get scheds, alive and no spare first
        scheds = []
        for sched_id in realm.schedulers:
            # Update the scheduler instance id with the scheduler uuid
            self.schedulers[sched_id].instance_id = sched_id
            scheds.append(self.schedulers[sched_id])

        # Now we sort the scheds so we take master, then spare
        # the dead, but we do not care about them
        scheds.sort(alive_then_spare_then_deads)
        scheds.reverse()  # pop is last, I need first
        return scheds

    def prepare_dispatch(self):
        """
        Prepare dispatch, so prepare for each daemon (schedulers, brokers, receivers, reactionners,
        pollers)

        :return: None
        """
        # Ok, we pass at least one time in dispatch, so now errors are True errors
        self.first_dispatch_done = True

        if self.dispatch_ok:
            return

        self.prepare_dispatch_schedulers()

        arbiters_cfg = {}
        for arb in self.arbiters:
            print("An arbiter to dispatch: %s" % arb)
            arbiters_cfg[arb.uuid] = arb.give_satellite_cfg()
            print("- : %s" % arbiters_cfg[arb.uuid])

        for realm in self.realms:
            print("A realm to dispatch: %s" % realm)
            for cfg in realm.confs.values():
                print("- cfg: %s" % cfg)
                for sat_type in ('reactionner', 'poller', 'broker', 'receiver'):
                    self.prepare_dispatch_other_satellites(sat_type, realm, cfg, arbiters_cfg)

    def prepare_dispatch_schedulers(self):
        """
        Prepare dispatch for schedulers

        :return: None
        """
        for realm in self.realms:
            conf_to_dispatch = [cfg for cfg in realm.confs.values() if not cfg.is_assigned]
            print("A configuration to dispatch: %s" % conf_to_dispatch)

            # Now we get in scheds all scheduler of this realm and upper so
            scheds = self.get_scheduler_ordered_list(realm)

            nb_conf = len(conf_to_dispatch)
            if nb_conf > 0:
                logger.info('[%s] Prepare dispatching for this realm', realm.get_name())
                logger.info('[%s] Prepare dispatching %d/%d configurations',
                            realm.get_name(), nb_conf, len(realm.confs))
                logger.info('[%s] Dispatching schedulers ordered as: %s',
                            realm.get_name(), ','.join([s.get_name() for s in scheds]))

            # prepare conf only for alive schedulers
            scheds = [s for s in scheds if s.alive]

            for conf in conf_to_dispatch:
                logger.info('[%s] Dispatching configuration %s', realm.get_name(), conf.uuid)

                # If there is no alive schedulers, not good...
                if not scheds:
                    logger.error('[%s] There are no alive schedulers in this realm!',
                                 realm.get_name())
                    break

                # we need to loop until the conf is assigned
                # or when there are no more schedulers available
                while True:
                    try:
                        sched = scheds.pop()
                    except IndexError:  # No more schedulers.. not good, no loop
                        # need_loop = False
                        # The conf does not need to be dispatch
                        cfg_id = conf.uuid
                        for sat_type in ('reactionner', 'poller', 'broker', 'receiver'):
                            realm.to_satellites[sat_type][cfg_id] = None
                            realm.to_satellites_need_dispatch[sat_type][cfg_id] = False
                            realm.to_satellites_managed_by[sat_type][cfg_id] = []
                        break

                    logger.info("[%s] Preparing configuration '%s' for the scheduler %s",
                                realm.get_name(), conf.uuid, sched.get_name())
                    if not sched.need_conf:
                        logger.info('[%s] The scheduler %s do not need any configuration, sorry',
                                    realm.get_name(), sched.get_name())
                        continue

                    # We give this configuration a new 'flavor'
                    conf.push_flavor = random.randint(1, 1000000)
                    satellites = realm.get_satellites_links_for_scheduler(self.pollers,
                                                                          self.reactionners,
                                                                          self.brokers)
                    conf_package = {
                        'conf': realm.serialized_confs[conf.uuid],
                        'override_conf': sched.get_override_configuration(),
                        'modules': sched.modules,
                        'arbiter_name': self.arbiter_link.name,
                        'alignak_name': conf.alignak_name,
                        'satellites': satellites,
                        'instance_name': sched.name,
                        'conf_uuid': conf.uuid,
                        'push_flavor': conf.push_flavor,
                        'skip_initial_broks': sched.skip_initial_broks,
                        'accept_passive_unknown_check_results':
                            sched.accept_passive_unknown_check_results,
                        # local statsd
                        'statsd_host': self.conf.statsd_host,
                        'statsd_port': self.conf.statsd_port,
                        'statsd_prefix': self.conf.statsd_prefix,
                        'statsd_enabled': self.conf.statsd_enabled,
                    }

                    sched.conf = conf
                    sched.conf_package = conf_package
                    sched.push_flavor = conf.push_flavor
                    sched.need_conf = False
                    sched.is_sent = False
                    conf.is_assigned = True
                    conf.assigned_to = sched

                    # We updated all data for this scheduler
                    pickled_conf = cPickle.dumps(conf_package)
                    logger.info('[%s] scheduler configuration %s size: %d bytes',
                                realm.get_name(), sched.name, sys.getsizeof(pickled_conf))
                    logger.info('[%s] configuration %s (%s) assigned to %s',
                                realm.get_name(), conf.uuid, conf.push_flavor, sched.name)
                    sched.managed_confs = {conf.uuid: conf.push_flavor}

                    # Now we generate the conf for satellites:
                    cfg_id = conf.uuid
                    sat_cfg = sched.give_satellite_cfg()
                    for sat_type in ('reactionner', 'poller', 'broker', 'receiver'):
                        realm.to_satellites[sat_type][cfg_id] = sat_cfg
                        realm.to_satellites_need_dispatch[sat_type][cfg_id] = True
                        realm.to_satellites_managed_by[sat_type][cfg_id] = []

                    # Special case for receiver because need to send it the hosts list
                    hnames = [h.get_name() for h in conf.hosts]
                    sat_cfg['hosts_names'] = hnames
                    realm.to_satellites['receiver'][cfg_id] = sat_cfg

                    # The config is prepared for a scheduler, no need check another scheduler
                    break

        nb_missed = len([cfg for cfg in self.conf.parts.values() if not cfg.is_assigned])
        if nb_missed > 0:
            logger.warning("All schedulers configurations are not dispatched, %d are missing",
                           nb_missed)
        else:
            logger.info("All schedulers configurations are dispatched :)")

        # Sched without conf in a dispatch ok are set to no need_conf
        # so they do not raise dispatch where no use
        for sched in self.schedulers.items.values():
            if sched.conf is None:
                # "so it do not ask anymore for conf"
                sched.need_conf = False

    def prepare_dispatch_other_satellites(self, sat_type, realm, cfg, arbiters_cfg):
        """
        Prepare dispatch of other satellites: reactionner, poller, broker and receiver

        :return:
        """

        if cfg.uuid not in realm.to_satellites_need_dispatch[sat_type]:
            return

        if not realm.to_satellites_need_dispatch[sat_type][cfg.uuid]:
            return

        # make copies of potential_react list for sort
        satellites = []
        for sat_id in realm.get_potential_satellites_by_type(sat_type):
            sat = getattr(self, "%ss" % sat_type)[sat_id]
            if sat.alive and sat.reachable:
                satellites.append(sat)

        if satellites:
            satellite_string = "[%s] Dispatching %s satellites ordered as: " % (
                realm.get_name(), sat_type)
            for sat in satellites:
                satellite_string += '%s (spare:%s), ' % (
                    sat.get_name(), str(sat.spare))
            logger.info(satellite_string)
        else:
            logger.info("[%s] No %s satellites", realm.get_name(), sat_type)

        conf_uuid = cfg.uuid
        # Now we dispatch cfg to every one ask for it
        nb_cfg_prepared = 0
        for sat in satellites:
            if nb_cfg_prepared >= realm.get_nb_of_must_have_satellites(sat_type):
                continue
            logger.info('[%s] Preparing configuration for the %s: %s',
                        realm.get_name(), sat_type, sat.get_name())
            sat.cfg['alignak_name'] = cfg.alignak_name
            sat.cfg['schedulers'][conf_uuid] = realm.to_satellites[sat_type][conf_uuid]
            if sat.manage_arbiters:
                sat.cfg['arbiters'] = arbiters_cfg

            # Brokers should have poller/reactionners links too
            if sat_type == "broker":
                realm.fill_broker_with_poller_reactionner_links(sat,
                                                                self.pollers,
                                                                self.reactionners,
                                                                self.receivers,
                                                                self.realms)
            sat.active = False
            sat.is_sent = False

            sat.known_conf_managed_push(conf_uuid, cfg.push_flavor)

            nb_cfg_prepared += 1
            realm.to_satellites_managed_by[sat_type][conf_uuid].append(sat)

        # I've got enough satellite, the next ones are considered spares
        if nb_cfg_prepared == realm.get_nb_of_must_have_satellites(sat_type):
            if satellites:
                logger.info("[%s] OK, no more %s needed", realm.get_name(), sat_type)
            realm.to_satellites_need_dispatch[sat_type][conf_uuid] = False

    def dispatch(self):
        """
        Send configuration to satellites

        :return: None
        """
        if self.dispatch_ok:
            return
        self.dispatch_ok = True
        for scheduler in self.schedulers:
            if scheduler.is_sent:
                continue
            t01 = time.time()
            logger.info('Sending configuration to scheduler %s', scheduler.get_name())
            is_sent = scheduler.put_conf(scheduler.conf_package)
            logger.debug("Conf is sent in %d", time.time() - t01)
            if not is_sent:
                logger.warning('Configuration sending error for scheduler %s', scheduler.get_name())
                self.dispatch_ok = False
            else:
                logger.info('Configuration sent to scheduler %s',
                            scheduler.get_name())
                scheduler.is_sent = True
        for sat_type in ('reactionner', 'poller', 'broker', 'receiver'):
            for satellite in self.satellites:
                if satellite.type == sat_type:
                    if satellite.is_sent:
                        continue
                    logger.info('Sending configuration to %s %s', sat_type, satellite.get_name())
                    is_sent = satellite.put_conf(satellite.cfg)
                    satellite.is_sent = is_sent
                    if not is_sent:
                        logger.warning("Configuration sending error for %s '%s'",
                                       sat_type, satellite.get_name())
                        self.dispatch_ok = False
                        continue
                    satellite.active = True

                    logger.info('Configuration sent to %s %s', sat_type, satellite.get_name())
