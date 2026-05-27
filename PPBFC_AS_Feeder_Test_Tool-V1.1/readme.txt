
// --------------------------------------

20260404: PPBFC_AS_Feeder_Test_Tool-V1.1
UPDATES:
Added a new BFC auto-calibration feature to automatically calibrate the clock accuracy of the BFC controller, improve serial communication stability to prevent garbled characters.
Note: You must update to the latest BFC firmware to enable the auto-calibration feature.

// --------------------------------------

This tool is a simple serial port client program that incorporates most commands for BFC controllers, enabling users to conveniently test and adjust AS feeders.


Usage:

1. Select the correct serial Port, choose the default Baud Rate of 19200, and click Connect;

2. Upon successful connection, the software will automatically send commands to attempt basic response from the controllers;

3. Select the controller port you need to test: Choose "BoardAddr" and "Port". The *Port-N* number will be automatically assigned. All subsequent commands target this Port-N;

4. Clicking the “Set Angle to” button directly controls the servo angle (default 180 degrees), useful for initial servo arm angle adjustment during AS feeder installation;

5. "Single Advance" executes a single feed cycle. "Activate" must be clicked first before using this. "Repeat Advance" can be clicked directly to repeatedly send Single Advance commands at the specified delay interval (in ms). Clicking again stops automatic sending. This is typically useful for testing and fine-tuning the AS feeder.

6. "Get All Settings" retrieves the configuration for all ports on the current controller; 

7. "Update Port-N Settings" updates the configuration for this selected Port-N based on the parameters below. When using AS2-08-P02, this function can be used to modify the port's C value to approximately 125.

8. The serial log window on the right displays all transmitted and received serial characters; The input field and Send button at the bottom allow manual send of commands.

9. Note: The program will not issue an error alert if the target fails to respond due to incorrect Port-N number configuration or hardware failure.

10. BFC Auto Calibration: Select the total number of BFC units installed in the system, then click the Auto Calibration button. The program will calibrate each controller in sequence. Once all calibration cycles are complete, the controllers will reboot and achieve more stable serial communication. Checking the "Forced" option will reset the previous calibration results and perform a recalibration.

// --------------------------------------

pandaplacer.com