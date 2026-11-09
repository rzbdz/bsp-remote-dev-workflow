- SpacemiT boards: on boards with FDL strapped, every reset enters BROM download mode, so a cold
  boot from flashed storage cannot be observed there.
- SpacemiT U-Boot prints board identity on the console (`TLV item: product_name = ...`);
  `fastboot oem config:read` answers OKAY but the fastboot CLI does not show the value.
