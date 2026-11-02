# This file was generated entirely by AI.
# It has not been reviewed line by line by a human.
# Plain, complete UCI resources for a simple 2.4 GHz mac80211 access point.
{
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
      uci = {
        network = {
          loopback = {
            type = "interface";
            options = {
              device = "lo";
              proto = "static";
              ipaddr = "127.0.0.1";
              netmask = "255.0.0.0";
            };
          };
          bridge = {
            type = "device";
            options = {
              name = "br-lan";
              type = "bridge";
              inherit ports;
            };
          };
          lan = {
            type = "interface";
            options = {
              device = "br-lan";
              proto = "dhcp";
            };
          };
        };
        wireless = {
          radio0 = {
            type = "wifi-device";
            options = {
              type = "mac80211";
              inherit phy country;
              band = "2g";
              channel = "auto";
              htmode = "HT20";
              disabled = "0";
            };
          };
          ap = {
            type = "wifi-iface";
            options = {
              device = "radio0";
              network = "lan";
              mode = "ap";
              inherit ssid;
              disabled = "0";
              encryption = "psk2";
              key.sops = {
                file = passwordFile;
                key = "wifi_password";
              };
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
