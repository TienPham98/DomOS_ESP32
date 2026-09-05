package mqtt

import (
	"encoding/json"
	"errors"
	"strings"
	"time"

	paho "github.com/eclipse/paho.mqtt.golang"

	"github.com/domos/backend-go/internal/devices"
	"github.com/domos/backend-go/pkg/logger"
)

var errNotConnected = errors.New("mqtt: client not connected")

// Broadcaster is the minimal websocket capability the MQTT client needs,
// so it can fan device state changes out to connected dashboard clients.
type Broadcaster interface {
	Broadcast(event string, payload interface{})
}

// Client wraps the Paho MQTT client with DomOS-specific publish helpers
// and a device-state subscriber that keeps Postgres and the websocket hub
// in sync with what devices report.
type Client struct {
	paho paho.Client
	repo *devices.Repository
	hub  Broadcaster
}

// Config holds connection settings for the MQTT broker.
type Config struct {
	Broker   string
	ClientID string
	Username string
	Password string
}

// New connects to the MQTT broker and returns a ready-to-use Client.
// Connection is retried by the underlying Paho client's auto-reconnect,
// so a broker that isn't up yet at boot won't crash the process.
func New(cfg Config, repo *devices.Repository, hub Broadcaster) (*Client, error) {
	c := &Client{repo: repo, hub: hub}

	opts := paho.NewClientOptions().
		AddBroker(cfg.Broker).
		SetClientID(cfg.ClientID).
		SetUsername(cfg.Username).
		SetPassword(cfg.Password).
		SetConnectTimeout(2 * time.Second).
		SetAutoReconnect(true).
		SetConnectRetry(true).
		SetConnectRetryInterval(5 * time.Second).
		SetOnConnectHandler(func(client paho.Client) {
			logger.Info.Println("mqtt: connected to broker")
			if token := client.Subscribe(TopicDeviceStateWildcard, 1, c.handleDeviceState); token.Wait() && token.Error() != nil {
				logger.Error.Printf("mqtt: failed to subscribe to %s: %v", TopicDeviceStateWildcard, token.Error())
			}
		}).
		SetConnectionLostHandler(func(client paho.Client, err error) {
			logger.Warn.Printf("mqtt: connection lost: %v", err)
		})

	c.paho = paho.NewClient(opts)

	go func() {
		if token := c.paho.Connect(); token.Wait() && token.Error() != nil {
			logger.Warn.Printf("mqtt: initial connect failed (broker offline): %v", token.Error())
		}
	}()

	return c, nil
}

// Publish marshals payload as JSON and publishes it (QoS 1, no retain)
// to the given topic. Implements devices.Publisher and ota.Publisher.
// Safe to call on a nil *Client (e.g. when the broker was unreachable at
// startup) -- it simply returns an error instead of panicking.
func (c *Client) Publish(topic string, payload interface{}) error {
	if c == nil || c.paho == nil {
		return errNotConnected
	}

	data, err := json.Marshal(payload)
	if err != nil {
		return err
	}
	token := c.paho.Publish(topic, 1, false, data)
	token.Wait()
	return token.Error()
}

// handleDeviceState processes domos/device/{id}/state messages, updating
// the device's online status in Postgres and broadcasting the change to
// any dashboard clients connected over websocket.
func (c *Client) handleDeviceState(_ paho.Client, msg paho.Message) {
	deviceID := extractDeviceID(msg.Topic())
	if deviceID == "" {
		return
	}

	var state devices.StatePayload
	if err := json.Unmarshal(msg.Payload(), &state); err != nil {
		logger.Warn.Printf("mqtt: failed to parse state payload from %s: %v", msg.Topic(), err)
		return
	}

	if c.repo != nil {
		if err := c.repo.SetOnlineState(deviceID, state.Online); err != nil {
			logger.Warn.Printf("mqtt: failed to persist state for device %s: %v", deviceID, err)
		}
	}

	if c.hub != nil {
		c.hub.Broadcast("device_state", map[string]interface{}{
			"device_id": deviceID,
			"online":    state.Online,
			"firmware":  state.Firmware,
			"ip":        state.IPAddress,
		})
	}
}

// extractDeviceID pulls {id} out of a domos/device/{id}/... topic.
func extractDeviceID(topic string) string {
	parts := strings.Split(topic, "/")
	if len(parts) < 3 {
		return ""
	}
	return parts[2]
}

// Close disconnects cleanly from the broker.
func (c *Client) Close() {
	if c.paho != nil && c.paho.IsConnected() {
		c.paho.Disconnect(250)
	}
}
