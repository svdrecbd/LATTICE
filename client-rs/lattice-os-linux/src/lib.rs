use socket2::{Domain, Protocol, Socket, Type};
use std::collections::HashMap;
use std::ffi::CStr;
use std::io;
use std::net::{IpAddr, Ipv4Addr, Ipv6Addr, SocketAddr, ToSocketAddrs};
use std::os::unix::io::{AsRawFd, RawFd};
use std::path::Path;
use std::time::{Duration, Instant};

pub struct UdpProber {
    socket: Socket,
    recv_buf: [u8; 2048],
    cmsg_buf: [u8; 256],
}

#[derive(Debug, Clone)]
pub struct UtunInterfaceInfo {
    pub name: String,
    pub flags: u32,
    pub has_non_loopback_addr: bool,
}

#[derive(Debug, Clone)]
pub struct UtunReport {
    pub present: bool,
    pub active: bool,
    pub interfaces: Vec<UtunInterfaceInfo>,
}

impl UdpProber {
    pub fn new(host: &str, port: u16, bind_ip: Option<IpAddr>) -> io::Result<Self> {
        let addr = resolve_first_for_family(host, port, bind_ip)?;
        let domain = match addr {
            SocketAddr::V4(_) => Domain::IPV4,
            SocketAddr::V6(_) => Domain::IPV6,
        };
        let socket = Socket::new(domain, Type::DGRAM, Some(Protocol::UDP))?;
        if let Some(ip) = bind_ip {
            let bind_addr = SocketAddr::new(ip, 0);
            socket.bind(&bind_addr.into())?;
        }
        socket.connect(&addr.into())?;

        enable_rx_timestamping(socket.as_raw_fd())?;

        Ok(Self {
            socket,
            recv_buf: [0u8; 2048],
            cmsg_buf: [0u8; 256],
        })
    }

    pub fn send_and_receive_rtt(
        &mut self,
        msg: &[u8],
        send_realtime_ns: u64,
        send_mono_ns: u64,
        timeout: Duration,
    ) -> io::Result<Option<f64>> {
        let fd = self.socket.as_raw_fd();
        let send_instant = Instant::now();
        let sent = unsafe { libc::send(fd, msg.as_ptr() as *const _, msg.len(), 0) };
        if sent < 0 {
            return Err(io::Error::last_os_error());
        }
        if sent as usize != msg.len() {
            return Err(io::Error::new(io::ErrorKind::Other, "short send"));
        }

        let deadline = Instant::now() + timeout;
        loop {
            let now = Instant::now();
            if now >= deadline {
                return Ok(None);
            }
            let remaining_ms = (deadline - now)
                .as_millis()
                .min(i32::MAX as u128) as i32;

            let mut pfd = libc::pollfd {
                fd,
                events: libc::POLLIN,
                revents: 0,
            };
            let rv = unsafe { libc::poll(&mut pfd, 1, remaining_ms) };
            if rv < 0 {
                return Err(io::Error::last_os_error());
            }
            if rv == 0 {
                return Ok(None);
            }
            if (pfd.revents & libc::POLLIN) == 0 {
                continue;
            }

            let (n, recv_ns) = self.recv_with_timestamp()?;
            if n != msg.len() {
                continue;
            }
            if &self.recv_buf[..n] != msg {
                continue;
            }
            let recv_instant = Instant::now();
            let fallback_rtt_ms = (recv_instant - send_instant).as_secs_f64() * 1000.0;

            let rtt_ms = choose_rtt_ms(recv_ns, send_realtime_ns, send_mono_ns)
                .unwrap_or(fallback_rtt_ms);
            return Ok(Some(rtt_ms));
        }
    }

    pub fn iface_name(&self) -> io::Result<String> {
        let addr = self
            .socket
            .local_addr()?
            .as_socket()
            .ok_or_else(|| io::Error::new(io::ErrorKind::Other, "non-IP socket"))?;
        iface_for_ip(addr.ip())
            .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "iface not found"))
    }

    pub fn local_addr(&self) -> io::Result<SocketAddr> {
        self.socket
            .local_addr()?
            .as_socket()
            .ok_or_else(|| io::Error::new(io::ErrorKind::Other, "non-IP socket"))
    }
}

pub fn iface_type(name: &str) -> String {
    if name == "lo" {
        return "loopback".into();
    }
    let wireless = Path::new("/sys/class/net")
        .join(name)
        .join("wireless");
    if wireless.exists() {
        return "wifi".into();
    }
    let net_type = Path::new("/sys/class/net").join(name).join("type");
    if let Ok(t) = std::fs::read_to_string(net_type) {
        if t.trim() == "1" {
            return "ethernet".into();
        }
    }
    if name.starts_with("ww") || name.starts_with("rmnet") {
        return "cellular".into();
    }
    "other".into()
}

pub fn utun_present() -> bool {
    utun_report().present
}

pub fn realtime_now_ns() -> u64 {
    unsafe {
        let mut ts: libc::timespec = std::mem::zeroed();
        if libc::clock_gettime(libc::CLOCK_REALTIME, &mut ts) != 0 {
            return 0;
        }
        (ts.tv_sec as u64) * 1_000_000_000u64 + (ts.tv_nsec as u64)
    }
}

pub fn monotonic_now_ns() -> u64 {
    unsafe {
        let mut ts: libc::timespec = std::mem::zeroed();
        if libc::clock_gettime(libc::CLOCK_MONOTONIC, &mut ts) != 0 {
            return 0;
        }
        (ts.tv_sec as u64) * 1_000_000_000u64 + (ts.tv_nsec as u64)
    }
}

pub fn utun_report() -> UtunReport {
    let mut map: HashMap<String, UtunInterfaceInfo> = HashMap::new();
    let mut ifap: *mut libc::ifaddrs = std::ptr::null_mut();
    let rv = unsafe { libc::getifaddrs(&mut ifap) };
    if rv != 0 {
        return UtunReport {
            present: false,
            active: false,
            interfaces: Vec::new(),
        };
    }
    let mut cur = ifap;
    unsafe {
        while !cur.is_null() {
            let ifa = &*cur;
            if !ifa.ifa_name.is_null() {
                let name = CStr::from_ptr(ifa.ifa_name)
                    .to_string_lossy()
                    .to_string();
                if name.starts_with("tun")
                    || name.starts_with("tap")
                    || name.starts_with("wg")
                    || name.starts_with("ppp")
                    || name.starts_with("ipsec")
                {
                    let entry = map.entry(name.clone()).or_insert(UtunInterfaceInfo {
                        name,
                        flags: ifa.ifa_flags as u32,
                        has_non_loopback_addr: false,
                    });
                    entry.flags = ifa.ifa_flags as u32;
                    if has_non_loopback_addr(ifa.ifa_addr) {
                        entry.has_non_loopback_addr = true;
                    }
                }
            }
            cur = ifa.ifa_next;
        }
        libc::freeifaddrs(ifap);
    }
    let interfaces: Vec<UtunInterfaceInfo> = map.into_values().collect();
    let active = interfaces.iter().any(|i| {
        (i.flags & (libc::IFF_UP as u32)) != 0
            && (i.flags & (libc::IFF_RUNNING as u32)) != 0
            && i.has_non_loopback_addr
    });
    UtunReport {
        present: !interfaces.is_empty(),
        active,
        interfaces,
    }
}

fn choose_rtt_ms(recv_ns: u64, send_realtime_ns: u64, send_mono_ns: u64) -> Option<f64> {
    const THRESH_NS: u64 = 5_000_000_000;

    let now_realtime = realtime_now_ns();
    if now_realtime > 0 && abs_diff(recv_ns, now_realtime) <= THRESH_NS {
        let diff = recv_ns.saturating_sub(send_realtime_ns);
        let rtt_ms = diff as f64 / 1_000_000.0;
        if rtt_ms.is_finite() && rtt_ms <= 60_000.0 {
            return Some(rtt_ms);
        }
    }

    let now_mono = monotonic_now_ns();
    if now_mono > 0 && abs_diff(recv_ns, now_mono) <= THRESH_NS {
        let diff = recv_ns.saturating_sub(send_mono_ns);
        let rtt_ms = diff as f64 / 1_000_000.0;
        if rtt_ms.is_finite() && rtt_ms <= 60_000.0 {
            return Some(rtt_ms);
        }
    }

    None
}

fn abs_diff(a: u64, b: u64) -> u64 {
    if a >= b { a - b } else { b - a }
}

fn resolve_first_for_family(
    host: &str,
    port: u16,
    bind_ip: Option<IpAddr>,
) -> io::Result<SocketAddr> {
    let mut addrs = (host, port).to_socket_addrs()?;
    if let Some(ip) = bind_ip {
        let want_v4 = ip.is_ipv4();
        for addr in addrs {
            if want_v4 && matches!(addr, SocketAddr::V4(_)) {
                return Ok(addr);
            }
            if !want_v4 && matches!(addr, SocketAddr::V6(_)) {
                return Ok(addr);
            }
        }
        return Err(io::Error::new(
            io::ErrorKind::NotFound,
            "no resolved addresses for bind family",
        ));
    }
    addrs
        .next()
        .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "no resolved addresses"))
}

pub fn iface_ips(name: &str) -> io::Result<Vec<IpAddr>> {
    let mut ifap: *mut libc::ifaddrs = std::ptr::null_mut();
    let rv = unsafe { libc::getifaddrs(&mut ifap) };
    if rv != 0 {
        return Err(io::Error::last_os_error());
    }
    let mut out = Vec::new();
    let mut cur = ifap;
    unsafe {
        while !cur.is_null() {
            let ifa = &*cur;
            if ifa.ifa_addr.is_null() {
                cur = ifa.ifa_next;
                continue;
            }
            if !ifa.ifa_name.is_null() {
                let if_name = CStr::from_ptr(ifa.ifa_name)
                    .to_string_lossy()
                    .to_string();
                if if_name == name {
                    let sa_family = (*ifa.ifa_addr).sa_family as i32;
                    if sa_family == libc::AF_INET {
                        let sa = *(ifa.ifa_addr as *const libc::sockaddr_in);
                        let addr = IpAddr::V4(Ipv4Addr::from(u32::from_be(sa.sin_addr.s_addr)));
                        out.push(addr);
                    } else if sa_family == libc::AF_INET6 {
                        let sa = *(ifa.ifa_addr as *const libc::sockaddr_in6);
                        let addr = IpAddr::V6(Ipv6Addr::from(sa.sin6_addr.s6_addr));
                        out.push(addr);
                    }
                }
            }
            cur = ifa.ifa_next;
        }
        libc::freeifaddrs(ifap);
    }
    Ok(out)
}

fn enable_rx_timestamping(fd: RawFd) -> io::Result<()> {
    let on: libc::c_int = 1;
    let rv = unsafe {
        libc::setsockopt(
            fd,
            libc::SOL_SOCKET,
            libc::SO_TIMESTAMPNS,
            &on as *const _ as *const _,
            std::mem::size_of_val(&on) as libc::socklen_t,
        )
    };
    if rv != 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

fn recv_timestamp_ns(msg: &libc::msghdr) -> Option<u64> {
    unsafe {
        let mut cmsg = cmsg_firsthdr(msg);
        while !cmsg.is_null() {
            let cmsg_ref = &*cmsg;
            if cmsg_ref.cmsg_level == libc::SOL_SOCKET
                && cmsg_ref.cmsg_type == libc::SCM_TIMESTAMPNS
            {
                let data = cmsg_data(cmsg) as *const libc::timespec;
                if !data.is_null() {
                    let ts = *data;
                    return Some((ts.tv_sec as u64) * 1_000_000_000u64 + (ts.tv_nsec as u64));
                }
            }
            cmsg = cmsg_nxthdr(msg, cmsg);
        }
    }
    None
}

impl UdpProber {
    fn recv_with_timestamp(&mut self) -> io::Result<(usize, u64)> {
        unsafe {
            let mut iov = libc::iovec {
                iov_base: self.recv_buf.as_mut_ptr() as *mut _,
                iov_len: self.recv_buf.len(),
            };
            let mut hdr: libc::msghdr = std::mem::zeroed();
            hdr.msg_iov = &mut iov;
            hdr.msg_iovlen = 1;
            hdr.msg_control = self.cmsg_buf.as_mut_ptr() as *mut _;
            hdr.msg_controllen = self.cmsg_buf.len();

            let n = libc::recvmsg(self.socket.as_raw_fd(), &mut hdr, 0);
            if n < 0 {
                return Err(io::Error::last_os_error());
            }
            let ts = recv_timestamp_ns(&hdr)
                .ok_or_else(|| io::Error::new(io::ErrorKind::Other, "missing timestamp"))?;
            Ok(n as usize, ts)
        }
    }
}

fn list_ifaces() -> io::Result<Vec<String>> {
    let mut ifap: *mut libc::ifaddrs = std::ptr::null_mut();
    let rv = unsafe { libc::getifaddrs(&mut ifap) };
    if rv != 0 {
        return Err(io::Error::last_os_error());
    }
    let mut out = Vec::new();
    let mut cur = ifap;
    unsafe {
        while !cur.is_null() {
            let ifa = &*cur;
            if !ifa.ifa_name.is_null() {
                let name = CStr::from_ptr(ifa.ifa_name)
                    .to_string_lossy()
                    .to_string();
                out.push(name);
            }
            cur = ifa.ifa_next;
        }
        libc::freeifaddrs(ifap);
    }
    Ok(out)
}

fn has_non_loopback_addr(addr: *const libc::sockaddr) -> bool {
    if addr.is_null() {
        return false;
    }
    unsafe {
        let sa_family = (*addr).sa_family as i32;
        if sa_family == libc::AF_INET {
            let sa = *(addr as *const libc::sockaddr_in);
            let ip = Ipv4Addr::from(u32::from_be(sa.sin_addr.s_addr));
            return !ip.is_loopback();
        } else if sa_family == libc::AF_INET6 {
            let sa = *(addr as *const libc::sockaddr_in6);
            let ip = Ipv6Addr::from(sa.sin6_addr.s6_addr);
            return !ip.is_loopback();
        }
    }
    false
}

fn iface_for_ip(ip: IpAddr) -> Option<String> {
    let mut ifap: *mut libc::ifaddrs = std::ptr::null_mut();
    let rv = unsafe { libc::getifaddrs(&mut ifap) };
    if rv != 0 {
        return None;
    }
    let mut cur = ifap;
    let mut found = None;
    unsafe {
        while !cur.is_null() {
            let ifa = &*cur;
            if ifa.ifa_addr.is_null() {
                cur = ifa.ifa_next;
                continue;
            }
            let sa_family = (*ifa.ifa_addr).sa_family as i32;
            if sa_family == libc::AF_INET {
                let sa = *(ifa.ifa_addr as *const libc::sockaddr_in);
                let addr = IpAddr::V4(Ipv4Addr::from(u32::from_be(sa.sin_addr.s_addr)));
                if addr == ip {
                    if !ifa.ifa_name.is_null() {
                        found = Some(
                            CStr::from_ptr(ifa.ifa_name)
                                .to_string_lossy()
                                .to_string(),
                        );
                        break;
                    }
                }
            } else if sa_family == libc::AF_INET6 {
                let sa = *(ifa.ifa_addr as *const libc::sockaddr_in6);
                let addr = IpAddr::V6(Ipv6Addr::from(sa.sin6_addr.s6_addr));
                if addr == ip {
                    if !ifa.ifa_name.is_null() {
                        found = Some(
                            CStr::from_ptr(ifa.ifa_name)
                                .to_string_lossy()
                                .to_string(),
                        );
                        break;
                    }
                }
            }
            cur = ifa.ifa_next;
        }
        libc::freeifaddrs(ifap);
    }
    found
}

unsafe fn cmsg_firsthdr(msg: &libc::msghdr) -> *mut libc::cmsghdr {
    if (msg.msg_controllen as usize) < std::mem::size_of::<libc::cmsghdr>() {
        std::ptr::null_mut()
    } else {
        msg.msg_control as *mut libc::cmsghdr
    }
}

unsafe fn cmsg_nxthdr(msg: &libc::msghdr, cmsg: *const libc::cmsghdr) -> *mut libc::cmsghdr {
    let next = (cmsg as *const u8).add(cmsg_align((*cmsg).cmsg_len as usize));
    let end = (msg.msg_control as *const u8).add(msg.msg_controllen as usize);
    if next.add(std::mem::size_of::<libc::cmsghdr>()) > end {
        std::ptr::null_mut()
    } else {
        next as *mut libc::cmsghdr
    }
}

fn cmsg_align(len: usize) -> usize {
    let align = std::mem::size_of::<usize>();
    (len + align - 1) & !(align - 1)
}

unsafe fn cmsg_data(cmsg: *const libc::cmsghdr) -> *const u8 {
    (cmsg as *const u8).add(cmsg_align(std::mem::size_of::<libc::cmsghdr>()))
}
