#!/usr/bin/env python3

from math import ceil
from struct import pack, unpack_from
from time import sleep
from Types import EventType, InfoType
import argparse
import LedPatterns
import simplepyble
import sys
from PIL import Image
from io import BytesIO

def pil_image_to_bytes(img: Image.Image, max_size_kb: int = None) -> bytearray:
    img_buffer = BytesIO()

    # Convert the image to RGB mode if it's in RGBA mode
    if img.mode == 'RGBA':
        img = img.convert('RGB')

    # Resize the image to 800x800 pixels
    img = img.resize((800, 800), Image.ANTIALIAS)

    def save_img_with_quality(quality):
        img_buffer.seek(0)
        img.save(img_buffer, format='JPEG', quality=quality)
        return img_buffer.tell() / 1024

    if max_size_kb is not None:
        low_quality, high_quality = 1, 100
        current_quality = 75
        closest_quality = current_quality
        min_target_size_kb = max_size_kb * 0.9

        while low_quality <= high_quality:
            output_size_kb = save_img_with_quality(current_quality)
            print ("current output quality:", current_quality, " current size:", output_size_kb)

            if output_size_kb <= max_size_kb and output_size_kb >= min_target_size_kb:
                closest_quality = current_quality
                break

            if output_size_kb > max_size_kb:
                high_quality = current_quality - 1
            else:
                low_quality = current_quality + 1

            current_quality = (low_quality + high_quality) // 2
            closest_quality = current_quality

        # Save the image with the closest_quality
        save_img_with_quality(closest_quality)
    else:
        img.save(img_buffer, format='JPEG')

    return bytearray(img_buffer.getvalue())

class InstaxBLE:
    def __init__(self,
                 device_address=None,
                 device_name=None,
                 print_enabled=False,
                 dummy_printer=True,
                 verbose=False,
                 quiet=False,
                 image_path=None):
        """
        Initialize the InstaxBLE class.
        deviceAddress: if specified, will only connect to a printer with this address.
        printEnabled: by default, actual printing is disabled to prevent misprints.
        """
        self.chunkSize = 1808
        self.printEnabled = print_enabled
        self.peripheral = None
        self.deviceName = device_name.upper() if device_name else None
        self.deviceAddress = device_address
        self.dummyPrinter = dummy_printer
        self.quiet = quiet
        self.image_path = image_path
        self.verbose = verbose if not self.quiet else False
        self.serviceUUID = '70954782-2d83-473d-9e5f-81e1d02d5273'
        self.writeCharUUID = '70954783-2d83-473d-9e5f-81e1d02d5273'
        self.notifyCharUUID = '70954784-2d83-473d-9e5f-81e1d02d5273'
        self.packetsForPrinting = []
        self.pos = (0, 0, 0, 0)
        self.batteryState = 0
        self.batteryPercentage = 0
        self.photosLeft = 0
        self.isCharging = False
        self.imageSize = (0, 0)

        adapters = simplepyble.Adapter.get_adapters()
        if len(adapters) == 0:
            if not self.quiet:
                sys.exit("No bluetooth adapters found (are they enabled?)")
            else:
                sys.exit()

        if len(adapters) > 1:
            self.log(f"Found multiple adapters: {', '.join([adapter.identifier() for adapter in adapters])}")
            self.log(f"Using the first one: {adapters[0].identifier()}")
        self.adapter = adapters[0]

    def log(self, msg):
        """ Print a debug message"""
        if self.verbose:
            print(msg)

    def parse_printer_response(self, event, packet):
        """ Parse the response packet and print the result """
        # todo: create parsers for the different types of responses
        # Placeholder for a later update
        if event == EventType.XYZ_AXIS_INFO:
            x, y, z, o = unpack_from('<hhhB', packet[6:-1])
            self.pos = (x, y, z, o)
        elif event == EventType.LED_PATTERN_SETTINGS:
            pass
        elif event == EventType.SUPPORT_FUNCTION_INFO:
            try:
                infoType = InfoType(packet[7])
            except ValueError:
                self.log(f'Unknown InfoType: {packet[7]}')
                return

            if infoType == InfoType.IMAGE_SUPPORT_INFO:
                w, h = unpack_from('>HH', packet[8:12])
                # self.log(self.prettify_bytearray(packet[8:12]))
                self.log(f'image size: {w}x{h}')
                self.imageSize = (w, h)
            elif infoType == InfoType.BATTERY_INFO:
                self.batteryState, self.batteryPercentage = unpack_from('>BB', packet[8:10])
                self.log(f'battery state: {self.batteryState}, battery percentage: {self.batteryPercentage}')
            elif infoType == InfoType.PRINTER_FUNCTION_INFO:
                dataByte = packet[8]
                self.photosLeft = dataByte & 15
                self.isCharging = (1 << 7) & dataByte >= 1
                self.log(f'photos left: {self.photosLeft}')
                if self.isCharging:
                    self.log('Printer is charging')
                else:
                    self.log('Printer is running on battery')
        else:
            self.log(f'Uncaught response from printer. Eventype: {event}')

    def notification_handler(self, packet):
        """ Gets called whenever the printer replies and handles parsing the received data """
        self.log('Notification handler:')
        self.log(f'\t{self.prettify_bytearray(packet[:40])}')
        if not self.quiet:
            if len(packet) < 8:
                self.log(f"\tError: response packet size should be >= 8 (was {len(packet)})!")
                return
            elif not self.validate_checksum(packet):
                self.log("\tResponse packet checksum was invalid!")
                return

        header, length, op1, op2 = unpack_from('<HHBB', packet)
        # self.log('\theader: ', header, '\t', self.prettify_bytearray(packet[0:2]))
        # self.log('\tlength: ', length, '\t', self.prettify_bytearray(packet[2:4]))
        # self.log('\top1: ', op1, '\t\t', self.prettify_bytearray(packet[4:5]))
        # self.log('\top2: ', op2, '\t\t', self.prettify_bytearray(packet[5:6]))

        try:
            event = EventType((op1, op2))
            self.log(f'\tevent: {event}')
        except ValueError:
            self.log(f"Unknown EventType: ({op1}, {op2})")
            return

        self.parse_printer_response(event, packet)

        if len(self.packetsForPrinting) > 0:
            self.log(f'Packets left to send: {len(self.packetsForPrinting)}')
            packet = self.packetsForPrinting.pop(0)
            self.send_packet(packet)
            if len(self.packetsForPrinting) % 10 == 0:
                self.log("sending packets:", len(self.packetsForPrinting))

    def connect(self, timeout=0):
        """ Connect to the printer. Stops trying after <timeout> seconds. """
        if self.dummyPrinter:
            return

        self.peripheral = self.find_device(timeout=timeout)
        if self.peripheral:
            try:
                self.log(f"\n\nConnecting to: {self.peripheral.identifier()} [{self.peripheral.address()}]")
                self.peripheral.connect()
            except Exception as e:
                if not self.quiet:
                    self.log(f'error on connecting: {e}')

            if self.peripheral.is_connected():
                self.log(f"Connected (mtu: {self.peripheral.mtu()})")
                self.log('Attaching notification_handler')
                try:
                    self.peripheral.notify(self.serviceUUID, self.notifyCharUUID, self.notification_handler)
                except Exception as e:
                    if not self.quiet:
                        self.log(f'error on attaching notification_handler: {e}')
                        return

                self.get_printer_info()

    def disconnect(self):
        """ Disconnect from the printer (if connected) """
        if self.dummyPrinter:
            return
        if self.peripheral:
            if self.peripheral.is_connected():
                self.log('Disconnecting...')
                self.peripheral.disconnect()

    def enable_printing(self):
        """ Enable printing. """
        self.printEnabled = True

    def disable_printing(self):
        """ Disable printing. """
        self.printEnabled = False

    def find_device(self, timeout=0):
        """" Scan for our device and return it when found """
        self.log('Looking for instax printer...')
        secondsTried = 0
        while True:
            self.adapter.scan_for(2000)
            peripherals = self.adapter.scan_get_results()
            for peripheral in peripherals:
                foundName = peripheral.identifier()
                foundAddress = peripheral.address()
                if foundName.startswith('INSTAX'):
                    self.log(f"Found: {foundName} [{foundAddress}]")
                if (self.deviceName and foundName.startswith(self.deviceName)) or \
                   (self.deviceAddress and foundAddress == self.deviceAddress) or \
                   (self.deviceName is None and self.deviceAddress is None and \
                   foundName.startswith('INSTAX-') and foundName.endswith('(IOS)')):
                    # if foundAddress.startswith('FA:AB:BC'):  # start of IOS endpooint
                    #     to convert to ANDROID endpoint, replace 'FA:AB:BC' with '88:B4:36')
                    if peripheral.is_connectable():
                        return peripheral
                    elif not self.quiet:
                        self.log(f"Printer at {foundAddress} is not connectable")
            secondsTried += 1
            if timeout != 0 and secondsTried >= timeout:
                return None

    def create_color_payload(self, colorArray, speed, repeat, when):
        """
        Create a payload for a color pattern. See send_led_pattern for details.
        """
        payload = pack('BBBB', when, len(colorArray), speed, repeat)
        for color in colorArray:
            payload += pack('BBB', color[0], color[1], color[2])
        return payload

    def send_led_pattern(self, pattern, speed=5, repeat=255, when=0):
        """ Send a LED pattern to the Instax printer.
            colorArray: array of BGR(!) values to use in animation, e.g. [[255, 0, 0], [0, 255, 0], [0, 0, 255]]
            speed: time per frame/color: higher is slower animation
            repeat: 0 = don't repeat (so play once), 1-254 = times to repeat, 255 = repeat forever
            when: 0 = normal, 1 = on print, 2 = on print completion, 3 = pattern switch """
        payload = self.create_color_payload(pattern, speed, repeat, when)
        packet = self.create_packet(EventType.LED_PATTERN_SETTINGS, payload)
        self.send_packet(packet)

    def prettify_bytearray(self, value):
        """ Helper funtion to convert a bytearray to a string of hex values. """
        return ' '.join([f'{x:02x}' for x in value])

    def create_checksum(self, bytearray):
        """ Create a checksum for a given packet. """
        return (255 - (sum(bytearray) & 255)) & 255

    def create_packet(self, eventType, payload=b''):
        """ Create a packet to send to the printer. """
        if isinstance(eventType, EventType):  # allows passing in an event or a value directly
            eventType = eventType.value

        header = b'\x41\x62'  # 'Ab' means client to printer, 'aB' means printer to client
        opCode = bytes([eventType[0], eventType[1]])
        packetSize = pack('>H', 7 + len(payload))
        packet = header + packetSize + opCode + payload
        packet += pack('B', self.create_checksum(packet))
        return packet

    def validate_checksum(self, packet):
        """ Validate the checksum of a packet. """
        return (sum(packet) & 255) == 255

    def send_packet(self, packet):
        """ Send a packet to the printer """
        if not self.dummyPrinter and not self.quiet:
            if not self.peripheral:
                self.log("no peripheral to send packet to")
            elif not self.peripheral.is_connected():
                self.log("peripheral not connected")

        header, length, op1, op2 = unpack_from('<HHBB', packet)
        try:
            event = EventType((op1, op2))
        except Exception:
            event = 'Unknown event'

        self.log(f'sending eventtype: {event}')

        smallPacketSize = 182
        numberOfParts = ceil(len(packet) / smallPacketSize)
        # self.log("number of packets to send: ", numberOfParts)
        for subPartIndex in range(numberOfParts):
            # self.log((subPartIndex + 1), '/', numberOfParts)
            subPacket = packet[subPartIndex * smallPacketSize:subPartIndex * smallPacketSize + smallPacketSize]

            if not self.dummyPrinter:
                self.peripheral.write_command(self.serviceUUID, self.writeCharUUID, subPacket)

    # TODO: printer doesn't seem to respond to this?
    # async def shut_down(self):
    #     """ Shut down the printer. """
    #     packet = self.create_packet(EventType.SHUT_DOWN)
    #     return self.send_packet(packet)

    def image_to_bytes(self, imagePath):
        """ Convert an image to a bytearray """
        imgdata = None
        try:
            # TODO: I think returning image.read() already returns bytes so no need for bytearray?
            with open(imagePath, "rb") as image:
                imgdata = bytearray(image.read())
            return imgdata
        except Exception as e:
            if not self.quiet:
                self.log(f'Error loading image: {e}')

    def print_image(self, imgSrc):
        """
        print an image. Either pass a path to an image (as a string) or pass
        the bytearray to print directly
        """
        if self.photosLeft == 0 and not self.dummyPrinter:
            self.log("Can't print: no photos left")
            return

        imgData = imgSrc
        if isinstance(imgSrc, str):  # if it's a path, load the image contents
            image = Image.open(imgSrc)
            image_byte_array = pil_image_to_bytes(image, max_size_kb=105)
            imgData = image_byte_array #self.image_to_bytes(imgSrc)
            self.log(f"len of imagedata: {len(imgData)}")

        self.packetsForPrinting = [
            self.create_packet(EventType.PRINT_IMAGE_DOWNLOAD_START, b'\x02\x00\x00\x00' + pack('>I', len(imgData)))
        ]

        # divide image data up into chunks of <chunkSize> bytes and pad the last chunk with zeroes if needed
        # chunkSize = 900
        chunkSize = 1808
        imgDataChunks = [imgData[i:i + chunkSize] for i in range(0, len(imgData), chunkSize)]
        if len(imgDataChunks[-1]) < chunkSize:
            imgDataChunks[-1] = imgDataChunks[-1] + bytes(chunkSize - len(imgDataChunks[-1]))

        # create a packet from each of our chunks, this includes adding the chunk number
        for index, chunk in enumerate(imgDataChunks):
            imgDataChunks[index] = pack('>I', index) + chunk  # add chunk number as int (4 bytes)
            self.packetsForPrinting.append(self.create_packet(EventType.PRINT_IMAGE_DOWNLOAD_DATA, imgDataChunks[index]))

        self.packetsForPrinting.append(self.create_packet(EventType.PRINT_IMAGE_DOWNLOAD_END))

        if self.printEnabled:
            self.packetsForPrinting.append(self.create_packet(EventType.PRINT_IMAGE))
            self.packetsForPrinting.append(self.create_packet((0, 2), b'\x02'))
        elif not self.quiet:
            self.log("Printing is disabled, sending all packets except the actual print command")

        # for packet in self.packetsForPrinting:
        #     self.log(self.prettify_bytearray(packet))
        # exit()
        # send the first packet from our list, the packet handler will take care of the rest

        if not self.dummyPrinter:
            packet = self.packetsForPrinting.pop(0)
            self.send_packet(packet)
            self.log("entering wait loop")
            try:
                while len(self.packetsForPrinting) > 0:
                    sleep(0.1)
            except KeyboardInterrupt:
                raise KeyboardInterrupt

    def print_services(self):
        """ Get and display and overview of the printer's services and characteristics """
        self.log("Successfully connected, listing services...")
        services = self.peripheral.services()
        service_characteristic_pair = []
        for service in services:
            for characteristic in service.characteristics():
                service_characteristic_pair.append((service.uuid(), characteristic.uuid()))

        for i, (service_uuid, characteristic) in enumerate(service_characteristic_pair):
            self.log(f"{i}: {service_uuid} {characteristic}")

    def get_printer_orientation(self):
        packet = self.create_packet(EventType.XYZ_AXIS_INFO)
        self.send_packet(packet)

    def get_printer_status(self):
        packet = self.create_packet(EventType.SUPPORT_FUNCTION_INFO, pack('>B', InfoType.PRINTER_FUNCTION_INFO.value))
        self.send_packet(packet)

    def get_printer_info(self):
        """ Get and display the printer's function info """
        self.log("Getting function info...")

        packet = self.create_packet(EventType.SUPPORT_FUNCTION_INFO, pack('>B', InfoType.IMAGE_SUPPORT_INFO.value))
        self.send_packet(packet)

        packet = self.create_packet(EventType.SUPPORT_FUNCTION_INFO, pack('>B', InfoType.BATTERY_INFO.value))
        self.send_packet(packet)

        self.get_printer_status()


def main(args={}):
    """ Example usage of the InstaxBLE class """
    instax = InstaxBLE(**args)
    try:
        # To prevent misprints during development this script sends all the
        # image data except the final 'go print' command. To enable printing
        # uncomment the next line, or pass --print-enabled when calling
        # this script

        # instax.enable_printing()

        instax.connect()

        # Set a rainbow effect to be shown while printing and a pulsating
        # green effect when printing is done
        instax.send_led_pattern(LedPatterns.rainbow, when=1)
        instax.send_led_pattern(LedPatterns.pulseGreen, when=2)

        # you can also read the current accelerometer values if you want
        # while True:
        #     instax.get_printer_orientation()
        #     sleep(.5)

        # send your image (.jpg) to the printer by
        # passing the image_path as an argument when calling
        # this script, or by specifying the path in your code
        if instax.image_path:
            instax.print_image(instax.image_path)
        else:
            if instax.imageSize == (600, 800):
                instax.print_image('example-mini.jpg')
            elif instax.imageSize == (800, 800):
                instax.print_image('example-square.jpg')
            elif instax.imageSize == (1260, 800):
                instax.print_image('example-wide.jpg')
            else:
                instax.log(f"Unknown image size requested by printer: {instax.imageSize}")

    except Exception as e:
        print(type(e).__name__, __file__, e.__traceback__.tb_lineno)
        instax.log(f'Error: {e}')
    finally:
        instax.disconnect()  # all done, disconnect


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('-a', '--device-address')
    parser.add_argument('-n', '--device-name')
    parser.add_argument('-p', '--print-enabled', action='store_true')
    parser.add_argument('-d', '--dummy-printer', action='store_true')
    parser.add_argument('-v', '--verbose', action='store_true')
    parser.add_argument('-q', '--quiet', action='store_true')
    parser.add_argument('-i', '--image-path', help='Path to the image file')
    args = parser.parse_args()

    main(vars(args))