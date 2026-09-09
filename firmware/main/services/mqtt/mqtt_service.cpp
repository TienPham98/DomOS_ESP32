#include "mqtt_service.h"

#include <cstring>

#include "esp_crt_bundle.h"
#include "esp_event.h"
#include "esp_log.h"
#include "kernel/event_bus.h"
#include "mqtt_client.h"

namespace {
constexpr char TAG[] = "mqtt";
}

bool MqttService::Start(EventBus *events)
{
    events_ = events;
    const char *uri = CONFIG_DOMOS_MQTT_URI;
    if (uri == nullptr || uri[0] == '\0') {
        ESP_LOGE(TAG, "MQTT broker URI is not configured");
        return false;
    }
    const bool secure_cloud = std::strncmp(uri, "mqtts://", 8) == 0 ||
                              std::strncmp(uri, "wss://", 6) == 0;
    if (secure_cloud &&
        (CONFIG_DOMOS_MQTT_USERNAME[0] == '\0' || CONFIG_DOMOS_MQTT_PASSWORD[0] == '\0')) {
        ESP_LOGW(TAG,
                 "Secure MQTT credentials are missing; broker disabled to protect voice connectivity");
        return false;
    }
    esp_mqtt_client_config_t config{};
    config.broker.address.uri = uri;
    if (secure_cloud) {
        config.broker.verification.crt_bundle_attach = esp_crt_bundle_attach;
    }
    config.credentials.client_id = "domos-es3c28p-01";
    if (CONFIG_DOMOS_MQTT_USERNAME[0] != '\0') {
        config.credentials.username = CONFIG_DOMOS_MQTT_USERNAME;
    }
    if (CONFIG_DOMOS_MQTT_PASSWORD[0] != '\0') {
        config.credentials.authentication.password = CONFIG_DOMOS_MQTT_PASSWORD;
    }
    client_ = esp_mqtt_client_init(&config);
    if (client_ == nullptr) return false;
    esp_mqtt_client_register_event(static_cast<esp_mqtt_client_handle_t>(client_), MQTT_EVENT_ANY,
                                   OnEvent, this);
    ESP_LOGI(TAG, "Connecting to MQTT Broker at %s", uri);
    return esp_mqtt_client_start(static_cast<esp_mqtt_client_handle_t>(client_)) == ESP_OK;
}

bool MqttService::Publish(const char *topic, const char *payload)
{
    if (!connected_ || client_ == nullptr || topic == nullptr || payload == nullptr) return false;
    return esp_mqtt_client_publish(static_cast<esp_mqtt_client_handle_t>(client_), topic, payload, 0, 1, 0) >= 0;
}

void MqttService::OnEvent(void *handler_args, esp_event_base_t, int32_t event_id, void *event_data)
{
    auto *service = static_cast<MqttService *>(handler_args);
    auto *event = static_cast<esp_mqtt_event_handle_t>(event_data);
    switch (event_id) {
    case MQTT_EVENT_CONNECTED:
        service->connected_ = true;
        ESP_LOGI(TAG, "MQTT Connected to Broker!");
        esp_mqtt_client_subscribe(event->client, "domos/device/es3c28p-01/#", 1);
        esp_mqtt_client_subscribe(event->client, "domos/server/broadcast", 1);
        esp_mqtt_client_subscribe(event->client, "dom/#", 1);
        if (service->events_) service->events_->Publish(EventType::MqttConnected, TAG, "connected");
        // Publish online heartbeat state
        esp_mqtt_client_publish(event->client, "domos/device/es3c28p-01/state", "{\"online\":true,\"state\":{\"power\":true}}", 0, 1, 0);
        break;
    case MQTT_EVENT_DISCONNECTED:
        service->connected_ = false;
        break;
    case MQTT_EVENT_DATA: {
        if (event->current_data_offset == 0) {
            service->incoming_topic_.assign(event->topic, event->topic_len);
            service->incoming_payload_.clear();
            service->incoming_payload_.reserve(event->total_data_len);
        }
        service->incoming_payload_.append(event->data, event->data_len);

        const bool complete = event->current_data_offset + event->data_len >= event->total_data_len;
        if (complete) {
            if (service->events_) {
                service->events_->Publish(EventType::MqttMessage, TAG,
                                          service->incoming_payload_.c_str());
            }
            if (service->message_handler_) {
                service->message_handler_(service->incoming_topic_.data(), service->incoming_topic_.size(),
                                          service->incoming_payload_.data(), service->incoming_payload_.size());
            }
        }
        break;
    }
    default:
        break;
    }
}
