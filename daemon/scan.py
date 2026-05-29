import asyncio
from bleak import BleakScanner

async def scan():
    print("Scanning for 10 seconds...")
    devices = await BleakScanner.discover(timeout=10.0)
    for d in devices:
        print(f"  Name: {d.name!r:30s}  Address: {d.address}")

asyncio.run(scan())