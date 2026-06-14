// SPDX-License-Identifier: GPL-2.0
//
// Egress filter for claude-isol's "--no-lan" mode: drop ALL traffic to the local
// networks so nothing -- TCP, UDP, ICMP, raw, anything -- reaches the LAN, while
// the rest of the internet stays reachable.
//
// cgroup_skb/egress runs once per packet for every socket under the cgroup (the
// podman container *and* the --local bwrap tree alike), with no dependency inside
// the image. It reads the real destination IP -- which pasta preserves -- and
// drops packets bound for private ranges.
//
// DNS is not special-cased: --no-lan pins the resolver to a public address
// (1.1.1.1), which passes the filter, so the LAN is blocked with no holes.
// Loopback and globally-routable destinations always pass.

#include <linux/bpf.h>
#include <linux/if_ether.h>
#include <linux/ip.h>
#include <linux/ipv6.h>
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_endian.h>

char LICENSE[] SEC("license") = "GPL";

#define ALLOW 1
#define DROP  0

// addr is in network byte order (as carried in the IP header); compare in host order.
static __always_inline int is_private_v4(__u32 addr_net)
{
	__u32 a = bpf_ntohl(addr_net);

	if ((a & 0xff000000) == 0x0a000000)  // 10.0.0.0/8
		return 1;
	if ((a & 0xfff00000) == 0xac100000)  // 172.16.0.0/12
		return 1;
	if ((a & 0xffff0000) == 0xc0a80000)  // 192.168.0.0/16
		return 1;
	if ((a & 0xffff0000) == 0xa9fe0000)  // 169.254.0.0/16 (link-local + metadata)
		return 1;
	return 0;
	// note: 127.0.0.0/8 deliberately NOT here -- loopback stays allowed.
}

// w0 is the first 32-bit word of the v6 destination, network byte order.
static __always_inline int is_private_v6(__u32 w0)
{
	__u32 h0 = bpf_ntohl(w0);

	if ((h0 & 0xfe000000) == 0xfc000000)  // fc00::/7  (unique local)
		return 1;
	if ((h0 & 0xffc00000) == 0xfe800000)  // fe80::/10 (link-local)
		return 1;
	return 0;
	// ::1 (loopback) and global unicast stay allowed.
}

SEC("cgroup_skb/egress")
int block_lan(struct __sk_buff *skb)
{
	void *data = (void *)(long)skb->data;
	void *data_end = (void *)(long)skb->data_end;

	if (skb->protocol == bpf_htons(ETH_P_IP)) {
		struct iphdr *ip = data;

		if ((void *)(ip + 1) > data_end)
			return ALLOW;  // too short to judge; don't drop blindly
		if (is_private_v4(ip->daddr))
			return DROP;
	} else if (skb->protocol == bpf_htons(ETH_P_IPV6)) {
		struct ipv6hdr *ip6 = data;

		if ((void *)(ip6 + 1) > data_end)
			return ALLOW;
		if (is_private_v6(ip6->daddr.in6_u.u6_addr32[0]))
			return DROP;
	}
	return ALLOW;
}