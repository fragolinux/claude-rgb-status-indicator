# Parked firmware

Working code for hardware that is not currently in use. Kept because it runs,
not because it is finished.

## supermini-trafficlight

ESP32-C3 SuperMini driving a 3-LED traffic-light module (green GPIO1, yellow
GPIO3, red GPIO4), same wire protocol and arbitration as the other firmware.

Parked because the module that was on hand is mispopulated: 4.7 kΩ in series
with the yellow LED instead of a few hundred ohms, which starves it to roughly
0.3 mA — invisible, and beyond anything firmware can compensate for. The
product photos for the same part show 330 Ω throughout, so a correctly built
module should work as written.

The mapping in this version already routes around the dead yellow, carrying its
meaning with green rhythms instead. On a good module, set `YELLOW_USABLE = True`
to get the intended three-colour behaviour back.
