# (c) 2015 - Jaguar Land Rover.
#
# Mozilla Public License 2.0
#
# Python dbus service that coordinates all use cases.
#
import gtk
import dbus.service
from dbus.mainloop.glib import DBusGMainLoop
import manifest_processor
import traceback
import sys
import getopt
import os
import swm
import settings
import logging

logger = logging.getLogger(settings.LOGGER)

#
# Define the DBUS-facing Software Loading Manager service
#
class SLMService(dbus.service.Object):
    def __init__(self, db_path):
        self.manifest_processor = manifest_processor.ManifestProcessor(db_path)
        # Define our own bus name
        bus_name = dbus.service.BusName('org.genivi.SoftwareLoadingManager', bus=dbus.SessionBus())        
        # Define our own object on the SoftwareLoadingManager bus
        dbus.service.Object.__init__(self, bus_name, "/org/genivi/SoftwareLoadingManager")

            
    def initiate_download(self, package_id):
        swm.dbus_method("org.genivi.SotaClient", "initiateDownload", package_id)

    # 
    # Distribute a report of a completed installation
    # to all involved parties. So far those parties are
    # the HMI and the SOTA client
    #
    def distribute_update_result(self, 
                                 update_id, 
                                 results):
        # Send installation report to HMI
        logger.debug('SoftwareLoadingManager.SLMService.distribute_update_result(%s): Sending report to Hmi.updateReport().', update_id)
        swm.dbus_method("org.genivi.Hmi", "updateReport", dbus.String(update_id), results)

        # Send installation report to SOTA
        logger.debug('SoftwareLoadingManager.SLMService.distribute_update_result(%s): Sending report to SotaClient.updateReport().', update_id)
        swm.dbus_method("org.genivi.SotaClient", "updateReport", dbus.String(update_id), results)

    def get_current_manifest(self):
        return self.manifest_processor.current_manifest

    def start_next_manifest(self):
        if not self.manifest_processor.load_next_manifest():
            return False

        manifest = self.get_current_manifest()
        self.inform_hmi_of_new_manifest(manifest)
        # 
        # This whole manifest may already have been executed and stored
        # as completed by the manifest_processor. If so, distribute the
        # given result
        # 
        if not manifest.start_next_operation():
            self.distribute_update_result(manifest.update_id,
                                          manifest.operation_results)
            # Recursively call self to engage next manifest.
            return self.start_next_manifest()

        self.inform_hmi_of_new_operation(manifest.active_operation)
        return True
        
    def inform_hmi_of_new_operation(self,op):
        swm.dbus_method("org.genivi.Hmi", "operationStarted",
                        op.operation_id, op.time_estimate, op.hmi_message)
        return None
    
    def inform_hmi_of_new_manifest(self,manifest):
        total_time = 0
        for op in manifest.operations:
            total_time = total_time + op.time_estimate

        swm.dbus_method("org.genivi.Hmi", "manifestStarted",
                        manifest.update_id, total_time, manifest.description)
        return None
    
    def start_next_operation(self):
        #
        # If we are currently not processing an image, load
        # the one we just queued.
        #
        
        #
        # No manifest loaded.
        # Load next manifest and, if successful, start the
        # fist operation in said manifest
        #
        manifest = self.get_current_manifest()
        if not manifest:
            return self.start_next_manifest()

        # We have an active manifest. Check if we have an active operation.
        # If so return success
        if  manifest.active_operation:
            return True


        # We have an active manifest, but no active operation.
        # Try to start the next operation.
        # If that fails, we are out of operations in the current
        # manifest. Try to load the next manifest, which
        # will also start its first operation.
        if not manifest.start_next_operation():
            return self.manifest_processor.load_next_manifest()

        # Inform HMI of active operation
        self.inform_hmi_of_new_operation(manifest.active_operation)
        return True

    @dbus.service.method("org.genivi.SoftwareLoadingManager",
                         async_callbacks=('send_reply', 'send_error'))
    def updateAvailable(self, 
                         update_id, 
                         description, 
                         signature,
                         request_confirmation,
                         send_reply,
                         send_error): 

        logger.debug('SoftwareLoadingManager.SLMService.updateAvailable(%s, %s, %s, %s): Called.',
                     update_id, description, signature, request_confirmation)
        
        # Send back an immediate reply since DBUS
        # doesn't like python dbus-invoked methods to do 
        # their own calls (nested calls).
        #
        send_reply(True)

        #
        # Send a notification to the HMI to get user approval / decline
        # Once user has responded, HMI will invoke self.package_confirmation()
        # to drive the use case forward.
        #
        if request_confirmation:
            logger.debug('SoftwareLoadingManager.SLMService.updateAvailable(): Called Hmi.updateNotification().')
            swm.dbus_method("org.genivi.Hmi", "updateNotification", update_id, description)
            return None

        logger.debug('SoftwareLoadingManager.SLMService.updateAvailable(): No user cnfirmation requested: initiating download.')
        self.initiate_download(update_id)
        return None


    @dbus.service.method("org.genivi.SoftwareLoadingManager",
                         async_callbacks=('send_reply', 'send_error'))
    def updateConfirmation(self,        
                           update_id, 
                           approved,
                           send_reply, 
                           send_error): 

        logger.debug('SoftwareLoadingManager.SLMService.updateConfirmation(%s, %s): Called.',
                     update_id, approved)
        
        #
        # Send back an immediate reply since DBUS
        # doesn't like python dbus-invoked methods to do 
        # their own calls (nested calls).
        #
        send_reply(True)

        if approved:
            #
            # Call the SOTA client and ask it to start the download.
            # Once the download is complete, SOTA client will call 
            # download complete on this process to actually
            # process the package.
            #
            logger.debug('SoftwareLoadingManager.SLMService.updateConfirmation(): Approved: Calling initiate_ownload().')
            self.initiate_download(update_id)
            logger.debug('SoftwareLoadingManager.SLMService.updateConfirmation(): Approved: Called SotaClient.initiateDownload().')
        else:
            # User did not approve. Send installation report
            logger.debug('SoftwareLoadingManager.SLMService.updateConfirmation(): Declined: Calling installation_report().')
            self.distribute_update_result(update_id, [
                swm.result('N/A',
                           swm.SWMResult.SWM_RES_USER_DECLINED,
                           "Installation declined by user")
            ]) 
            logger.debug('SoftwareLoadingManager.SLMService.updateConfirmation(): Declined: Called SotaClient.installationReport().')

        return None

    @dbus.service.method("org.genivi.SoftwareLoadingManager", 
                         async_callbacks=('send_reply', 'send_error'))
    def downloadComplete(self,
                          update_image,
                          signature,
                          send_reply,
                          send_error): 
            
        logger.debug('SoftwareLoadingManager.SLMService.downloadComplete(%s, %s): Called.',
                     update_image, signature)
        
        #
        # Send back an immediate reply since DBUS
        # doesn't like python dbus-invoked methods to do 
        # their own calls (nested calls).
        #
        send_reply(True)
        logger.warning('SoftwareLoadingManager.SLMService.downloadComplete(): FIXME: Check signature of update image.')

        #
        # Queue the image.
        #
        try:
            self.manifest_processor.queue_image(update_image)
            self.start_next_operation()
        except Exception as e:
            logger.error('SoftwareLoadingManager.SLMService.downloadComplete(): Failed to process downloaded update: %s.', e)
        return None

    #
    # Receive and process a installation report.
    # Called by package_manager, partition_manager, or ecu_module_loader
    # once they have completed their process_update() calls invoked
    # by SoftwareLoadingManager
    #
    @dbus.service.method("org.genivi.SoftwareLoadingManager",
                         async_callbacks=('send_reply', 'send_error'))
    def operationResult(self, 
                         transaction_id, 
                         result_code, 
                         result_text,
                         send_reply,
                         send_error): 

        logger.debug('SoftwareLoadingManager.SLMService.operationResult(%s, %s, %s): Called.',
                     transaction_id, result_code, result_text)
        
        try:
            # Send back an immediate reply since DBUS
            # doesn't like python dbus-invoked methods to do 
            # their own calls (nested calls).
            #
            send_reply(True)

            manifest = self.get_current_manifest()
            if not manifest:
                logger.warning('SoftwareLoadingManager.SLMService.operationResult(): No manifest to handle callback reply.')
                return None

            manifest.complete_transaction(transaction_id, result_code, result_text)
            if not self.start_next_operation():
                self.distribute_update_result(manifest.update_id,
                                              manifest.operation_results)
        except Exception as e:
            logger.error('SoftwareLoadingManager.SLMService.operationResult(): Failed to process operation result: %s.', e)
            traceback.print_exc()
        return None

    @dbus.service.method("org.genivi.SoftwareLoadingManager")
    def getInstalledPackages(self, include_packegs, include_module_firmware): 
        logger.debug('SoftwareLoadingManager.SLMService.getInstalledPackages(%s, %s): Called.',
                     include_packegs, include_module_firmware)
        
        return [ "bluez_driver", "bluez_apps" ]

def usage():
    print "Usage:", sys.argv[0], "[-r] [-d database_file] "
    print
    print "  -r                Reset the completed operations database prior to running"
    print "  -d database_file  Path to database file to store completed operations in"
    print
    print "Example:", sys.argv[0],"-r -d /tmp/database"
    sys.exit(255)


logger.debug('Software Loading Manager - Running')

try:  
    opts, args= getopt.getopt(sys.argv[1:], "rd:")

except getopt.GetoptError:
    print "Could not parse arguments."
    usage()

db_path = "/tmp/completed_operations.json"
reset_db = False

for o, a in opts:
    if o == "-r":
        reset_db = True
    elif o == "-d":
        db_path = a
    else:
        print "Unknown option: {}".format(o)
        usage()

DBusGMainLoop(set_as_default=True)

# Check if we are to delete the old database.
if reset_db:
    try:
        os.remove(db_path)
    except:
        pass


slm_sota = SLMService(db_path)

while True:
    gtk.main_iteration()
