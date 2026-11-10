# Plain, complete UCI resources for a simple 2.4 GHz mac80211 access point.
{
  lib,
  targetHost,
  ssid,
  passwordFile,
  ports,
  phy,
  country,
}:
{
  services.openwrt = {
    enable = true;
    devices.router = {
      target_host = targetHost;
      resolver.target = "/tmp/resolv.conf.d/resolv.conf.auto";
      uci = lib.uci {
        network = {
          interface.loopback = {
            device = "lo";
            proto = "static";
            ipaddr = "127.0.0.1";
            netmask = "255.0.0.0";
          };
          device.bridge = {
            name = "br-lan";
            type = "bridge";
            inherit ports;
          };
          interface.lan = {
            device = "br-lan";
            proto = "dhcp";
          };
        };
        wireless = {
          wifi-device.radio0 = {
            type = "mac80211";
            inherit phy country;
            band = "2g";
            channel = "auto";
            htmode = "HT20";
            disabled = false;
          };
          wifi-iface.ap = {
            device = "radio0";
            network = "lan";
            mode = "ap";
            inherit ssid;
            disabled = false;
            encryption = "psk2";
            key.sops = {
              file = passwordFile;
              key = "wifi_password";
            };
          };
        };
      };
      services = {
        network = {
          action = "reload";
          reloadOn.uci = [
            "network"
            "wireless"
          ];
        };
        dnsmasq = {
          enable = false;
          running = false;
        };
        odhcpd = {
          enable = false;
          running = false;
        };
      };
    };
  };
}
