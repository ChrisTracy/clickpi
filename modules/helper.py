def is_raspberry_pi():
    try:
        with open("/proc/cpuinfo", "r") as f:
            cpuinfo = f.read()
            # Check if "Raspberry Pi" appears in the hardware or revision information
            if "Raspberry Pi" in cpuinfo or "BCM" in cpuinfo:
                return True
            else:
                return False
    except FileNotFoundError:
        return False  # Not a Linux system or the file doesn't exist