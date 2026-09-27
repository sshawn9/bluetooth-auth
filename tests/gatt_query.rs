//! Exercise actual GATT ReadValue calls on an isolated D-Bus, without Bluetooth.
#![cfg(target_os = "linux")]

use std::{
    fs,
    process::Command,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
        mpsc,
    },
    thread,
    time::Duration,
};

use dbus::{
    arg::PropMap,
    blocking::Connection,
    channel::{Channel, MatchingReceiver},
    message::MatchRule,
};
use dbus_crossroads::Crossroads;
use serde_json::{Value, json};

mod support;
use support::PrivateBus;

struct Characteristic {
    uuid: &'static str,
    value: Arc<Mutex<Vec<u8>>>,
    reads: Arc<Mutex<Vec<&'static str>>>,
}

#[test]
fn queries_selected_remote_values_and_rejects_unusable_connections_or_values() {
    let bus = PrivateBus::start();
    let address_file = bus.directory.join("address");
    fs::write(&address_file, "AA:BB:CC:DD:EE:FF\n").unwrap();
    let connected = Arc::new(AtomicBool::new(true));
    let stop = Arc::new(AtomicBool::new(false));
    let reads = Arc::new(Mutex::new(Vec::new()));
    let battery = Arc::new(Mutex::new(vec![73]));
    // Bluetooth SIG example: 2010-12-18 15:23:06.750, Saturday.
    let cts = Arc::new(Mutex::new(vec![0xda, 0x07, 12, 18, 15, 23, 6, 6, 192, 2]));
    let (ready_tx, ready_rx) = mpsc::channel();
    let thread = {
        let bus_address = bus.address.clone();
        let connected = connected.clone();
        let stop = stop.clone();
        let reads = reads.clone();
        let battery = battery.clone();
        let cts = cts.clone();
        thread::spawn(move || {
            let mut channel = Channel::open_private(&bus_address).unwrap();
            channel.register().unwrap();
            let connection = Connection::from(channel);
            connection
                .request_name("org.bluez", false, true, false)
                .unwrap();
            let mut crossroads = Crossroads::new();
            let device = crossroads.register("org.bluez.Device1", move |builder| {
                builder
                    .property::<String, _>("Address")
                    .get(|_, _: &mut ()| Ok("AA:BB:CC:DD:EE:FF".into()));
                builder
                    .property::<bool, _>("Connected")
                    .get(move |_, _: &mut ()| Ok(connected.load(Ordering::Acquire)));
                builder
                    .property::<bool, _>("ServicesResolved")
                    .get(|_, _: &mut ()| Ok(true));
            });
            let service = crossroads.register("org.bluez.GattService1", |builder| {
                builder
                    .property::<String, _>("UUID")
                    .get(|_, uuid: &mut String| Ok(uuid.clone()));
            });
            let characteristic = crossroads.register("org.bluez.GattCharacteristic1", |builder| {
                builder
                    .property::<String, _>("UUID")
                    .get(|_, data: &mut Characteristic| Ok(data.uuid.into()));
                builder
                    .property::<Vec<u8>, _>("Value")
                    .get(|_, _: &mut Characteristic| Ok(vec![0])); // Deliberately stale cache.
                builder.method(
                    "ReadValue",
                    ("options",),
                    ("value",),
                    |_, data: &mut Characteristic, (_options,): (PropMap,)| {
                        data.reads.lock().unwrap().push(data.uuid);
                        Ok((data.value.lock().unwrap().clone(),))
                    },
                );
            });
            crossroads.insert("/", &[crossroads.object_manager()], ());
            // The object path differs from the resolved identity address.
            let path = format!(
                "/org/bluez/hci0/dev_{}",
                bluer::Address([0; 6]).to_string().replace(':', "_")
            );
            crossroads.insert(path.clone(), &[device], ());
            for (id, service_uuid, characteristic_uuid, value) in [
                (
                    "0001",
                    "0000180f-0000-1000-8000-00805f9b34fb",
                    "00002a19-0000-1000-8000-00805f9b34fb",
                    battery,
                ),
                (
                    "0003",
                    "00001805-0000-1000-8000-00805f9b34fb",
                    "00002a2b-0000-1000-8000-00805f9b34fb",
                    cts,
                ),
            ] {
                crossroads.insert(
                    format!("{path}/service{id}"),
                    &[service],
                    service_uuid.to_owned(),
                );
                crossroads.insert(
                    format!("{path}/service{id}/char0002"),
                    &[characteristic],
                    Characteristic {
                        uuid: characteristic_uuid,
                        value,
                        reads: reads.clone(),
                    },
                );
            }
            connection.start_receive(
                MatchRule::new_method_call(),
                Box::new(move |message, connection| {
                    crossroads.handle_message(message, connection).unwrap();
                    true
                }),
            );
            ready_tx.send(()).unwrap();
            while !stop.load(Ordering::Acquire) {
                connection.process(Duration::from_millis(20)).unwrap();
            }
        })
    };
    ready_rx.recv_timeout(Duration::from_secs(2)).unwrap();
    let query = |args: &[&str]| {
        Command::new(env!("CARGO_BIN_EXE_bluetooth-auth-gatt-query"))
            .env("DBUS_SYSTEM_BUS_ADDRESS", &bus.address)
            .arg("--address-file")
            .arg(&address_file)
            .args(args)
            .output()
            .unwrap()
    };
    let expected_cts = json!({
        "year": 2010, "month": 12, "day": 18,
        "hours": 15, "minutes": 23, "seconds": 6,
        "day_of_week": 6, "fractions256": 192, "adjust_reason": 2,
    });
    for (args, expected) in [
        (vec![], json!({})),
        (vec!["--battery"], json!({"battery_percent": 73})),
        (vec!["--cts"], json!({"cts": expected_cts})),
        (
            vec!["--battery", "--cts"],
            json!({"battery_percent": 73, "cts": expected_cts}),
        ),
    ] {
        reads.lock().unwrap().clear();
        let output = query(&args);
        assert!(output.status.success(), "{:?}", output);
        assert_eq!(
            serde_json::from_slice::<Value>(&output.stdout).unwrap(),
            expected
        );
        let expected_reads = expected.as_object().unwrap().len();
        assert_eq!(reads.lock().unwrap().len(), expected_reads);
    }

    connected.store(false, Ordering::Release);
    reads.lock().unwrap().clear();
    let output = query(&["--battery"]);
    assert_eq!(output.status.code(), Some(1));
    assert!(String::from_utf8_lossy(&output.stderr).contains("not connected"));
    assert!(reads.lock().unwrap().is_empty());
    connected.store(true, Ordering::Release);

    battery.lock().unwrap()[0] = 101;
    cts.lock().unwrap().pop();
    for flag in ["--battery", "--cts"] {
        let output = query(&[flag]);
        assert_eq!(output.status.code(), Some(1));
        assert!(String::from_utf8_lossy(&output.stderr).contains("Invalid"));
    }
    stop.store(true, Ordering::Release);
    thread.join().unwrap();
}
