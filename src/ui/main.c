#include "lvgl.h"

#include <X11/Xlib.h>
#include <X11/cursorfont.h>
#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

/*
 * CMS driver display, 1280x640.
 *
 * The screen draws application state. It does not score risk.
 * LIVE and CABIN are the driver views. SYSTEM is the service view.
 *
 * Type: 16 captions, 20 values, 28 critical titles, 36 driver status.
 * Space: 16 margin, 12 gap, 8 radius. No shadow, no gradient.
 * Severity, everywhere: safe, info, attention, warning, critical, fault.
 */

#define UI_W 1280
#define UI_H 640
#define HEADER_H 56
#define FOOTER_H 64
#define VIEW_Y HEADER_H
#define VIEW_H (UI_H - HEADER_H - FOOTER_H)
#define PREVIEW_W 640
#define PREVIEW_H 360
#define PORT 9750
#define FIELD_CHARS 160

#define COL_BG 0x0E1216
#define COL_HEADER 0x12171D
#define COL_SURFACE 0x171C22
#define COL_SURFACE_2 0x1E252E
#define COL_LINE 0x2C3540
#define COL_TEXT 0xF4F6F8
#define COL_MUTED 0x9AA6B4
#define COL_SOS 0xB71C1C

enum {
    VIEW_LIVE = 0,
    VIEW_CABIN,
    VIEW_SYSTEM,
    VIEW_COUNT
};

enum {
    SEV_SAFE = 0,
    SEV_INFO,
    SEV_ATTENTION,
    SEV_WARNING,
    SEV_CRITICAL,
    SEV_FAULT,
    SEV_COUNT
};

enum {
    F_MODE = 0,
    F_CAMERA,
    F_MIC,
    F_GPS,
    F_DMS,
    F_OCCUPANCY,
    F_SEVERITY,
    F_ALERT,
    F_VLM,
    F_CLIP,
    F_MESSAGE,
    F_FPS,
    F_FRONT,
    F_REAR,
    F_PHONE,
    F_REACH,
    F_EYES,
    F_GAZE,
    F_YAWN,
    F_VLM_TEXT,
    F_SAFETY,
    F_HEADLINE,
    F_TITLE,
    F_DETAIL,
    F_ACTION,
    F_DRIVER,
    F_ATTENTION,
    F_DROWSY,
    F_ACTIVITY,
    F_SYSTEM,
    F_LINK,
    F_RECORD,
    F_MODEL,
    F_LATENCY,
    F_CABIN,
    F_COUNT
};

static const uint32_t SEV_FILL[SEV_COUNT] = {
    0x3CCB7F, 0x6AA8E8, 0xE2B340, 0xF08A2A, 0xE23D3D, 0xE4D2B0,
};
static const uint32_t SEV_TEXT[SEV_COUNT] = {
    0x5EE0A0, 0x9CC8F8, 0xF0C45A, 0xFFB15A, 0xFF6B6B, 0xF6E7C8,
};
static const uint32_t SEV_BANNER[SEV_COUNT] = {
    0x14241C, 0x15202C, 0x2A230F, 0x2A1A0C, 0x2C1214, 0x1C1814,
};

static lv_obj_t *screen_root;
static lv_obj_t *accent;
static lv_obj_t *safety_dot;
static lv_obj_t *safety_label;
static lv_obj_t *clock_label;
static lv_obj_t *view_live;
static lv_obj_t *view_cabin;
static lv_obj_t *view_system;
static lv_obj_t *hero;
static lv_obj_t *presence;
static lv_obj_t *metric_value[5];
static lv_obj_t *live_occ;
static lv_obj_t *live_front;
static lv_obj_t *live_rear;
static lv_obj_t *live_phone;
static lv_obj_t *live_reach;
static lv_obj_t *live_cabin;
static lv_obj_t *sys_dot;
static lv_obj_t *sys_word;
static lv_obj_t *row_camera_dot;
static lv_obj_t *row_camera;
static lv_obj_t *row_driver_dot;
static lv_obj_t *row_driver;
static lv_obj_t *row_cabin_dot;
static lv_obj_t *row_cabin;
static lv_obj_t *cabin_occ;
static lv_obj_t *cabin_sentence;
static lv_obj_t *cabin_front;
static lv_obj_t *cabin_rear;
static lv_obj_t *cabin_phone;
static lv_obj_t *cabin_reach;
static lv_obj_t *cabin_image_state;
static lv_obj_t *banner;
static lv_obj_t *banner_bar;
static lv_obj_t *banner_title;
static lv_obj_t *banner_detail;
static lv_obj_t *banner_action;
static lv_obj_t *banner_time;
static lv_obj_t *ack_btn;
static lv_obj_t *footer;
static lv_obj_t *sos_btn;
static lv_obj_t *nav_btn[3];
static lv_obj_t *nav_label[3];
static lv_obj_t *eng_camera;
static lv_obj_t *eng_mic;
static lv_obj_t *eng_gps;
static lv_obj_t *eng_model;
static lv_obj_t *eng_fps;
static lv_obj_t *eng_latency;
static lv_obj_t *eng_record;
static lv_obj_t *eng_link;
static lv_obj_t *eng_vlm;
static lv_obj_t *eng_note;
static lv_obj_t *eng_decision;
static lv_obj_t *live_view;
static uint8_t live_buf[PREVIEW_W * PREVIEW_H * 4] __attribute__((aligned(64)));
static struct timespec live_mtim;
static int preview_ready;
static int current_view = -1;
static int banner_on;
static int last_level = -1;
static uint32_t alert_since;
static char shown_title[FIELD_CHARS];
static char field_text[F_COUNT][FIELD_CHARS];
static char clock_buf[8];
static int sock_fd = -1;
static char acc[4096];
static size_t acc_len;
static int sos_pressed;
static uint32_t incident_seen_at;

typedef struct {
    char name[64];
    char id[32];
    char phone[32];
    char license_no[32];
    char license_class[16];
    char expiry[16];
    char contact[64];
    char contact_phone[32];
    char vehicle[32];
    char shift[16];
    char note[64];
} DriverProfile;

typedef struct {
    char name[64];
    char id[32];
    char phone[32];
    char seat[16];
    char contact[64];
    char contact_phone[32];
} PassengerProfile;

static DriverProfile driver_profile = {
    "Sample Driver", "DRV-1042", "+1 555 0100", "D-1042", "C", "2027-04-30",
    "Sample Contact", "+1 555 0199", "CAB-18", "06:00", "Must wear glasses",
};
static PassengerProfile passenger_profile = {
    "Sample Passenger", "PAX-220", "+1 555 0133", "Rear", "Sample Contact", "+1 555 0177",
};

static void show_view(int index);
static void render_state(void);

static void mkdir_results(void)
{
    mkdir("results", 0755);
    mkdir("results/ui_shots", 0755);
}

static void write_cmd(const char *cmd)
{
    FILE *file = fopen("results/ui_cmd.txt", "w");
    if(!file) return;
    fprintf(file, "%s\n", cmd);
    fclose(file);
}

static void write_dms_view(int shown)
{
    FILE *file = fopen("results/dms_view.txt.tmp", "w");
    if(!file) return;
    fprintf(file, "%d\n", shown ? 1 : 0);
    fclose(file);
    rename("results/dms_view.txt.tmp", "results/dms_view.txt");
}

static void on_sos(lv_event_t *event)
{
    LV_UNUSED(event);
    write_cmd("sos");
}

static void on_ack(lv_event_t *event)
{
    LV_UNUSED(event);
    write_cmd("dismiss");
}

static void on_nav(lv_event_t *event)
{
    show_view((int)(intptr_t)lv_event_get_user_data(event));
}

static int set_label(lv_obj_t *obj, const char *text)
{
    const char *now = lv_label_get_text(obj);
    if(now && strcmp(now, text) == 0) return 0;
    lv_label_set_text(obj, text);
    return 1;
}

static void paint_text(lv_obj_t *obj, const char *text, uint32_t color)
{
    lv_color_t next = lv_color_hex(color);
    int changed = set_label(obj, text);
    if(!changed && lv_color_eq(lv_obj_get_style_text_color(obj, LV_PART_MAIN), next)) return;
    lv_obj_set_style_text_color(obj, next, 0);
}

static void paint_dot(lv_obj_t *obj, uint32_t color)
{
    lv_color_t next = lv_color_hex(color);
    if(lv_color_eq(lv_obj_get_style_bg_color(obj, LV_PART_MAIN), next)) return;
    lv_obj_set_style_bg_color(obj, next, 0);
}

static int severity_from(const char *token)
{
    if(strcmp(token, "SAFE") == 0) return SEV_SAFE;
    if(strcmp(token, "INFO") == 0) return SEV_INFO;
    if(strcmp(token, "ATTENTION") == 0) return SEV_ATTENTION;
    if(strcmp(token, "WARNING") == 0) return SEV_WARNING;
    if(strcmp(token, "CRITICAL") == 0) return SEV_CRITICAL;
    if(strcmp(token, "FAULT") == 0) return SEV_FAULT;
    return SEV_INFO;
}

static uint32_t reading_color(const char *text)
{
    if(strcmp(text, "CLOSED") == 0 || strcmp(text, "HIGH") == 0 || strcmp(text, "IN USE") == 0 ||
       strcmp(text, "YES") == 0 || strcmp(text, "YAWN") == 0)
        return SEV_TEXT[SEV_WARNING];
    if(strcmp(text, "AWAY") == 0 || strcmp(text, "PHONE") == 0 || strcmp(text, "DRINK") == 0 ||
       strcmp(text, "SMOKE") == 0 || strcmp(text, "DISTRACTED") == 0)
        return SEV_TEXT[SEV_ATTENTION];
    if(strcmp(text, "--") == 0 || strcmp(text, "UNKNOWN") == 0) return COL_MUTED;
    return COL_TEXT;
}

static uint32_t status_color(const char *text)
{
    if(strcmp(text, "OK") == 0 || strcmp(text, "CALM") == 0 || strcmp(text, "MONITORING") == 0 ||
       strcmp(text, "SAVED") == 0 || strcmp(text, "LIVE") == 0 || strcmp(text, "SENT") == 0 ||
       strcmp(text, "READY") == 0)
        return SEV_TEXT[SEV_SAFE];
    if(strcmp(text, "CHECKING") == 0 || strcmp(text, "LOADING") == 0)
        return SEV_TEXT[SEV_INFO];
    if(strcmp(text, "DEGRADED") == 0 || strcmp(text, "HELD") == 0 || strcmp(text, "PHONE") == 0 ||
       strcmp(text, "REACHING") == 0 || strcmp(text, "POSTURE") == 0 || strcmp(text, "OBJECT") == 0 ||
       strcmp(text, "RECORDING") == 0)
        return SEV_TEXT[SEV_WARNING];
    if(strcmp(text, "CONFLICT") == 0) return SEV_TEXT[SEV_CRITICAL];
    if(strcmp(text, "FAULT") == 0 || strcmp(text, "FAILED") == 0 || strcmp(text, "FAIL") == 0 ||
       strcmp(text, "OFFLINE") == 0)
        return SEV_TEXT[SEV_FAULT];
    return COL_MUTED;
}

static const char *cabin_sentence_for(const char *token)
{
    if(strcmp(token, "CALM") == 0) return "Cabin is calm";
    if(strcmp(token, "PHONE") == 0) return "Phone use in the cabin";
    if(strcmp(token, "REACHING") == 0) return "Reaching detected";
    if(strcmp(token, "CHECKING") == 0) return "Checking the cabin";
    if(strcmp(token, "CONFLICT") == 0) return "Disturbance in the cabin";
    if(strcmp(token, "OBJECT") == 0) return "Unusual object in the cabin";
    if(strcmp(token, "POSTURE") == 0) return "Unusual posture in the cabin";
    if(strcmp(token, "OFFLINE") == 0) return "Camera unavailable";
    if(strcmp(token, "STANDBY") == 0) return "Cabin check has not started";
    return "Cabin status unavailable";
}

static uint32_t hero_color(int level, const char *headline)
{
    if(strcmp(headline, "SEARCHING") == 0 || strcmp(headline, "STARTING") == 0)
        return SEV_TEXT[SEV_INFO];
    return SEV_TEXT[level];
}

static void flat(lv_obj_t *obj, uint32_t bg)
{
    lv_obj_remove_flag(obj, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_style_bg_color(obj, lv_color_hex(bg), 0);
    lv_obj_set_style_bg_opa(obj, LV_OPA_COVER, 0);
    lv_obj_set_style_border_width(obj, 0, 0);
    lv_obj_set_style_radius(obj, 0, 0);
    lv_obj_set_style_pad_all(obj, 0, 0);
    lv_obj_set_style_shadow_width(obj, 0, 0);
}

static lv_obj_t *make_card(lv_obj_t *parent, int x, int y, int w, int h)
{
    lv_obj_t *card = lv_obj_create(parent);
    lv_obj_set_pos(card, x, y);
    lv_obj_set_size(card, w, h);
    flat(card, COL_SURFACE);
    lv_obj_set_style_radius(card, 8, 0);
    lv_obj_set_style_border_color(card, lv_color_hex(COL_LINE), 0);
    lv_obj_set_style_border_width(card, 1, 0);
    return card;
}

static lv_obj_t *make_label(lv_obj_t *parent, const char *text, const lv_font_t *font, uint32_t color, int x, int y)
{
    lv_obj_t *label = lv_label_create(parent);
    lv_label_set_text(label, text);
    lv_obj_set_style_text_font(label, font, 0);
    lv_obj_set_style_text_color(label, lv_color_hex(color), 0);
    lv_obj_set_pos(label, x, y);
    return label;
}

static lv_obj_t *make_dot(lv_obj_t *parent, int x, int y, uint32_t color)
{
    lv_obj_t *dot = lv_obj_create(parent);
    lv_obj_set_size(dot, 10, 10);
    lv_obj_set_pos(dot, x, y);
    flat(dot, color);
    lv_obj_set_style_radius(dot, LV_RADIUS_CIRCLE, 0);
    lv_obj_remove_flag(dot, LV_OBJ_FLAG_CLICKABLE);
    return dot;
}

static lv_obj_t *make_button(lv_obj_t *parent, const char *text, int x, int y, int w, int h,
                             uint32_t bg, const lv_font_t *font, lv_event_cb_t cb, void *user,
                             lv_obj_t **label_slot)
{
    lv_obj_t *btn = lv_button_create(parent);
    lv_obj_set_pos(btn, x, y);
    lv_obj_set_size(btn, w, h);
    lv_obj_set_style_bg_color(btn, lv_color_hex(bg), 0);
    lv_obj_set_style_bg_color(btn, lv_color_hex(bg), LV_STATE_PRESSED);
    lv_obj_set_style_bg_opa(btn, LV_OPA_COVER, 0);
    lv_obj_set_style_bg_opa(btn, LV_OPA_COVER, LV_STATE_PRESSED);
    lv_obj_set_style_radius(btn, 8, 0);
    lv_obj_set_style_shadow_width(btn, 0, 0);
    lv_obj_set_style_shadow_width(btn, 0, LV_STATE_PRESSED);
    lv_obj_set_style_border_width(btn, 0, 0);
    lv_obj_set_style_outline_width(btn, 0, 0);
    lv_obj_set_style_pad_all(btn, 0, 0);
    lv_obj_add_event_cb(btn, cb, LV_EVENT_CLICKED, user);
    lv_obj_t *label = lv_label_create(btn);
    lv_label_set_text(label, text);
    lv_obj_set_style_text_color(label, lv_color_hex(COL_TEXT), 0);
    lv_obj_set_style_text_font(label, font, 0);
    lv_obj_center(label);
    if(label_slot) *label_slot = label;
    return btn;
}

static lv_obj_t *make_metric(lv_obj_t *parent, int x, int y, int w, int h, const char *caption, lv_obj_t **value)
{
    lv_obj_t *card = make_card(parent, x, y, w, h);
    lv_obj_set_style_bg_color(card, lv_color_hex(COL_SURFACE_2), 0);
    int caption_y = h >= 100 ? 18 : 12;
    int value_y = h >= 100 ? 56 : 40;
    make_label(card, caption, &lv_font_montserrat_16, COL_MUTED, 14, caption_y);
    *value = make_label(card, "--", &lv_font_montserrat_20, COL_TEXT, 14, value_y);
    return card;
}

static void paint_reading(lv_obj_t *label, const char *text)
{
    paint_text(label, text, reading_color(text));
}

static void paint_status(lv_obj_t *label, const char *text)
{
    paint_text(label, text, status_color(text));
}

static void paint_sensor(lv_obj_t *label, const char *raw, int optional)
{
    const char *text = "UNKNOWN";
    uint32_t color = COL_MUTED;
    if(strcmp(raw, "ok") == 0) {
        text = "OK";
        color = SEV_TEXT[SEV_SAFE];
    } else if(strcmp(raw, "down") == 0) {
        text = optional ? "UNAVAILABLE" : "OFFLINE";
        color = optional ? COL_MUTED : SEV_TEXT[SEV_FAULT];
    } else if(strcmp(raw, "no_fix") == 0) {
        text = "NO FIX";
        color = COL_MUTED;
    }
    paint_text(label, text, color);
}

static void show_banner(int on)
{
    if(on == banner_on) return;
    banner_on = on;
    if(on) {
        lv_obj_remove_flag(banner, LV_OBJ_FLAG_HIDDEN);
        lv_obj_fade_in(banner, 160, 0);
        alert_since = lv_tick_get();
        if(alert_since == 0) alert_since = 1;
    } else {
        lv_obj_add_flag(banner, LV_OBJ_FLAG_HIDDEN);
        lv_obj_set_style_opa(banner, LV_OPA_COVER, 0);
    }
}

static void layout_banner(int level)
{
    int critical = level == SEV_CRITICAL;
    int tall = level == SEV_WARNING || level == SEV_FAULT;
    int height = critical ? 152 : tall ? 148 : 116;
    lv_obj_set_height(banner, height);
    lv_obj_set_height(banner_bar, height);
    lv_obj_set_style_bg_color(banner, lv_color_hex(SEV_BANNER[level]), 0);
    lv_obj_set_style_border_color(banner, lv_color_hex(SEV_FILL[level]), 0);
    paint_dot(banner_bar, SEV_FILL[level]);
    lv_obj_set_width(banner_bar, 6);
    lv_obj_set_style_radius(banner_bar, 0, 0);
    lv_obj_set_style_text_font(banner_title, critical ? &lv_font_montserrat_28 : &lv_font_montserrat_20, 0);
    lv_obj_set_pos(banner_title, 28, 16);
    lv_obj_set_pos(banner_detail, 28, critical ? 56 : 50);
    lv_obj_set_pos(banner_action, 28, critical ? 88 : 82);
    lv_obj_set_pos(banner_time, 1088, 18);
    if(level == SEV_WARNING || level == SEV_CRITICAL)
        lv_obj_remove_flag(ack_btn, LV_OBJ_FLAG_HIDDEN);
    else
        lv_obj_add_flag(ack_btn, LV_OBJ_FLAG_HIDDEN);
}

static void style_nav(int index)
{
    const char *names[3] = {"LIVE", "CABIN", "SYSTEM"};
    for(int i = 0; i < 3; i++) {
        int on = i == index;
        uint32_t bg = on ? COL_SURFACE_2 : COL_HEADER;
        lv_obj_set_style_bg_color(nav_btn[i], lv_color_hex(bg), 0);
        lv_obj_set_style_bg_color(nav_btn[i], lv_color_hex(bg), LV_STATE_PRESSED);
        paint_text(nav_label[i], names[i], on ? COL_TEXT : COL_MUTED);
    }
}

static void show_view(int index)
{
    if(index < 0 || index >= VIEW_COUNT || index == current_view) return;
    current_view = index;
    if(index == VIEW_LIVE) lv_obj_remove_flag(view_live, LV_OBJ_FLAG_HIDDEN);
    else lv_obj_add_flag(view_live, LV_OBJ_FLAG_HIDDEN);
    if(index == VIEW_CABIN) lv_obj_remove_flag(view_cabin, LV_OBJ_FLAG_HIDDEN);
    else lv_obj_add_flag(view_cabin, LV_OBJ_FLAG_HIDDEN);
    if(index == VIEW_SYSTEM) lv_obj_remove_flag(view_system, LV_OBJ_FLAG_HIDDEN);
    else lv_obj_add_flag(view_system, LV_OBJ_FLAG_HIDDEN);
    style_nav(index);
    lv_obj_move_foreground(banner);
    lv_obj_move_foreground(footer);
}

static void copy_field(char *dst, size_t size, const char *src)
{
    snprintf(dst, size, "%s", src ? src : "");
}

static int split_bar(char *line, char **out, int max)
{
    int count = 1;
    out[0] = line;
    for(char *cursor = line; *cursor; cursor++) {
        if(*cursor == '|') {
            *cursor = '\0';
            if(count < max) out[count] = cursor + 1;
            count++;
        }
    }
    return count;
}

static void load_roster(void)
{
    FILE *file = fopen("fleet/roster.txt", "r");
    if(!file) return;
    char line[512];
    while(fgets(line, sizeof(line), file)) {
        char *start = line;
        while(*start == ' ' || *start == '\t') start++;
        if(*start == '#' || *start == '\n' || *start == '\0') continue;
        char *end = start + strlen(start);
        while(end > start && (end[-1] == '\n' || end[-1] == '\r')) {
            *--end = '\0';
        }
        char *fields[16];
        int count = split_bar(start, fields, 16);
        if(strcmp(fields[0], "driver") == 0 && count >= 12) {
            copy_field(driver_profile.name, sizeof(driver_profile.name), fields[1]);
            copy_field(driver_profile.id, sizeof(driver_profile.id), fields[2]);
            copy_field(driver_profile.phone, sizeof(driver_profile.phone), fields[3]);
            copy_field(driver_profile.license_no, sizeof(driver_profile.license_no), fields[4]);
            copy_field(driver_profile.license_class, sizeof(driver_profile.license_class), fields[5]);
            copy_field(driver_profile.expiry, sizeof(driver_profile.expiry), fields[6]);
            copy_field(driver_profile.contact, sizeof(driver_profile.contact), fields[7]);
            copy_field(driver_profile.contact_phone, sizeof(driver_profile.contact_phone), fields[8]);
            copy_field(driver_profile.vehicle, sizeof(driver_profile.vehicle), fields[9]);
            copy_field(driver_profile.shift, sizeof(driver_profile.shift), fields[10]);
            copy_field(driver_profile.note, sizeof(driver_profile.note), fields[11]);
        } else if(strcmp(fields[0], "passenger") == 0 && count >= 7) {
            copy_field(passenger_profile.name, sizeof(passenger_profile.name), fields[1]);
            copy_field(passenger_profile.id, sizeof(passenger_profile.id), fields[2]);
            copy_field(passenger_profile.phone, sizeof(passenger_profile.phone), fields[3]);
            copy_field(passenger_profile.seat, sizeof(passenger_profile.seat), fields[4]);
            copy_field(passenger_profile.contact, sizeof(passenger_profile.contact), fields[5]);
            copy_field(passenger_profile.contact_phone, sizeof(passenger_profile.contact_phone), fields[6]);
        }
    }
    fclose(file);
}

static void build_header(void)
{
    lv_obj_t *header = lv_obj_create(screen_root);
    lv_obj_set_pos(header, 0, 0);
    lv_obj_set_size(header, UI_W, HEADER_H);
    flat(header, COL_HEADER);
    lv_obj_set_style_border_side(header, LV_BORDER_SIDE_BOTTOM, 0);
    lv_obj_set_style_border_width(header, 1, 0);
    lv_obj_set_style_border_color(header, lv_color_hex(COL_LINE), 0);

    accent = make_dot(header, 0, 0, SEV_FILL[SEV_INFO]);
    lv_obj_set_pos(accent, 0, 0);
    lv_obj_set_size(accent, UI_W, 4);
    lv_obj_set_style_radius(accent, 0, 0);

    make_label(header, "CMS", &lv_font_montserrat_20, COL_TEXT, 20, 16);
    safety_dot = make_dot(header, 92, 23, SEV_FILL[SEV_INFO]);
    safety_label = make_label(header, "INFO", &lv_font_montserrat_16, SEV_TEXT[SEV_INFO], 110, 18);

    clock_label = make_label(header, "--:--", &lv_font_montserrat_20, COL_TEXT, UI_W - 104, 16);
    lv_obj_set_width(clock_label, 80);
    lv_obj_set_style_text_align(clock_label, LV_TEXT_ALIGN_RIGHT, 0);
}

static void build_live(void)
{
    view_live = lv_obj_create(screen_root);
    lv_obj_set_pos(view_live, 0, VIEW_Y);
    lv_obj_set_size(view_live, UI_W, VIEW_H);
    flat(view_live, COL_BG);

    lv_obj_t *driver = make_card(view_live, 16, 12, 804, 496);
    make_label(driver, "DRIVER", &lv_font_montserrat_16, COL_MUTED, 24, 20);
    hero = make_label(driver, "STARTING", &lv_font_montserrat_36, SEV_TEXT[SEV_INFO], 24, 52);
    presence = make_label(driver, "Looking for the driver", &lv_font_montserrat_16, COL_MUTED, 24, 104);
    lv_obj_t *rule = lv_obj_create(driver);
    lv_obj_set_pos(rule, 24, 348);
    lv_obj_set_size(rule, 756, 1);
    flat(rule, COL_LINE);
    lv_obj_remove_flag(rule, LV_OBJ_FLAG_CLICKABLE);

    const char *captions[5] = {"EYES", "GAZE", "DROWSINESS", "YAWN", "ACTIVITY"};
    for(int i = 0; i < 5; i++)
        make_metric(driver, 16 + i * 156, 364, 148, 112, captions[i], &metric_value[i]);

    lv_obj_t *cabin = make_card(view_live, 832, 12, 432, 348);
    make_label(cabin, "CABIN", &lv_font_montserrat_16, COL_MUTED, 20, 18);
    live_occ = make_label(cabin, "0", &lv_font_montserrat_36, COL_TEXT, 20, 48);
    make_label(cabin, "OCCUPANTS", &lv_font_montserrat_16, COL_MUTED, 96, 66);
    live_cabin = make_label(cabin, "Cabin check has not started", &lv_font_montserrat_16, COL_MUTED, 20, 112);
    lv_obj_set_width(live_cabin, 392);
    make_metric(cabin, 16, 156, 194, 80, "FRONT", &live_front);
    make_metric(cabin, 222, 156, 194, 80, "REAR", &live_rear);
    make_metric(cabin, 16, 248, 194, 80, "PHONE", &live_phone);
    make_metric(cabin, 222, 248, 194, 80, "REACHING", &live_reach);

    lv_obj_t *system = make_card(view_live, 832, 372, 432, 136);
    sys_dot = make_dot(system, 20, 18, SEV_FILL[SEV_INFO]);
    sys_word = make_label(system, "READY", &lv_font_montserrat_16, SEV_TEXT[SEV_INFO], 38, 14);
    row_camera_dot = make_dot(system, 20, 52, SEV_FILL[SEV_INFO]);
    make_label(system, "Camera", &lv_font_montserrat_16, COL_MUTED, 38, 48);
    row_camera = make_label(system, "--", &lv_font_montserrat_16, COL_MUTED, 276, 48);
    lv_obj_set_width(row_camera, 140);
    lv_obj_set_style_text_align(row_camera, LV_TEXT_ALIGN_RIGHT, 0);
    row_driver_dot = make_dot(system, 20, 82, SEV_FILL[SEV_INFO]);
    make_label(system, "Driver", &lv_font_montserrat_16, COL_MUTED, 38, 78);
    row_driver = make_label(system, "--", &lv_font_montserrat_16, COL_MUTED, 276, 78);
    lv_obj_set_width(row_driver, 140);
    lv_obj_set_style_text_align(row_driver, LV_TEXT_ALIGN_RIGHT, 0);
    row_cabin_dot = make_dot(system, 20, 112, COL_MUTED);
    make_label(system, "Cabin", &lv_font_montserrat_16, COL_MUTED, 38, 108);
    row_cabin = make_label(system, "STANDBY", &lv_font_montserrat_16, COL_MUTED, 276, 108);
    lv_obj_set_width(row_cabin, 140);
    lv_obj_set_style_text_align(row_cabin, LV_TEXT_ALIGN_RIGHT, 0);
}

static void build_cabin(void)
{
    view_cabin = lv_obj_create(screen_root);
    lv_obj_set_pos(view_cabin, 0, VIEW_Y);
    lv_obj_set_size(view_cabin, UI_W, VIEW_H);
    flat(view_cabin, COL_BG);

    lv_obj_t *well = make_card(view_cabin, 24, 72, 656, 376);
    for(size_t i = 0; i < sizeof(live_buf); i += 4) {
        live_buf[i] = 0x22;
        live_buf[i + 1] = 0x1C;
        live_buf[i + 2] = 0x17;
        live_buf[i + 3] = 0xFF;
    }
    live_view = lv_canvas_create(well);
    lv_canvas_set_buffer(live_view, live_buf, PREVIEW_W, PREVIEW_H, LV_COLOR_FORMAT_XRGB8888);
    lv_obj_set_pos(live_view, 8, 8);
    lv_obj_set_style_border_width(live_view, 0, 0);
    lv_obj_remove_flag(live_view, LV_OBJ_FLAG_CLICKABLE);

    lv_obj_t *panel = make_card(view_cabin, 696, 12, 568, 496);
    make_label(panel, "CABIN", &lv_font_montserrat_16, COL_MUTED, 24, 20);
    cabin_occ = make_label(panel, "0", &lv_font_montserrat_36, COL_TEXT, 24, 52);
    make_label(panel, "OCCUPANTS", &lv_font_montserrat_16, COL_MUTED, 100, 70);
    cabin_sentence = make_label(panel, "Cabin check has not started", &lv_font_montserrat_16, COL_MUTED, 24, 120);
    lv_obj_set_width(cabin_sentence, 520);
    make_metric(panel, 24, 176, 254, 100, "FRONT", &cabin_front);
    make_metric(panel, 290, 176, 254, 100, "REAR", &cabin_rear);
    make_metric(panel, 24, 288, 254, 100, "PHONE", &cabin_phone);
    make_metric(panel, 290, 288, 254, 100, "REACHING", &cabin_reach);
    cabin_image_state = make_label(panel, "Waiting for image", &lv_font_montserrat_16, COL_MUTED, 24, 440);
}

static void identity_card(lv_obj_t *parent, int y, int h, const char *caption, const char *name,
                          const char *meta, const char *extra)
{
    lv_obj_t *card = make_card(parent, 0, y, 488, h);
    make_label(card, caption, &lv_font_montserrat_16, COL_MUTED, 20, 16);
    lv_obj_t *title = make_label(card, name, &lv_font_montserrat_20, COL_TEXT, 20, 48);
    lv_obj_set_width(title, 448);
    lv_label_set_long_mode(title, LV_LABEL_LONG_DOT);
    lv_obj_t *line = make_label(card, meta, &lv_font_montserrat_16, COL_TEXT, 20, 84);
    lv_obj_set_width(line, 448);
    lv_label_set_long_mode(line, LV_LABEL_LONG_DOT);
    lv_obj_t *note = make_label(card, extra, &lv_font_montserrat_16, COL_MUTED, 20, 112);
    lv_obj_set_width(note, 448);
    lv_label_set_long_mode(note, LV_LABEL_LONG_DOT);
}

static void build_system(void)
{
    view_system = lv_obj_create(screen_root);
    lv_obj_set_pos(view_system, 0, VIEW_Y);
    lv_obj_set_size(view_system, UI_W, VIEW_H);
    flat(view_system, COL_BG);

    const char *captions[8] = {
        "CAMERA", "MICROPHONE", "POSITIONING", "DRIVER MODEL",
        "FRAME RATE", "LATENCY", "RECORDING", "LINK",
    };
    lv_obj_t **slots[8] = {
        &eng_camera, &eng_mic, &eng_gps, &eng_model,
        &eng_fps, &eng_latency, &eng_record, &eng_link,
    };
    for(int i = 0; i < 8; i++) {
        int col = i % 2;
        int row = i / 2;
        make_metric(view_system, 16 + col * 378, 16 + row * 124, 366, 112, captions[i], slots[i]);
    }

    lv_obj_t *side = lv_obj_create(view_system);
    lv_obj_set_pos(side, 776, 16);
    lv_obj_set_size(side, 488, 484);
    flat(side, COL_BG);

    lv_obj_t *vision = make_card(side, 0, 0, 488, 168);
    make_label(vision, "CABIN MODEL", &lv_font_montserrat_16, COL_MUTED, 20, 16);
    eng_vlm = make_label(vision, "idle", &lv_font_montserrat_20, COL_TEXT, 20, 48);
    eng_note = make_label(vision, "No cabin note", &lv_font_montserrat_16, COL_MUTED, 20, 84);
    lv_obj_set_width(eng_note, 448);
    lv_label_set_long_mode(eng_note, LV_LABEL_LONG_DOT);
    eng_decision = make_label(vision, "No decision yet", &lv_font_montserrat_16, COL_MUTED, 20, 116);
    lv_obj_set_width(eng_decision, 448);
    lv_label_set_long_mode(eng_decision, LV_LABEL_LONG_DOT);

    char meta[96];
    char extra[96];
    snprintf(meta, sizeof(meta), "%s  /  %s", driver_profile.vehicle, driver_profile.shift);
    snprintf(extra, sizeof(extra), "Contact  %s", driver_profile.contact);
    identity_card(side, 180, 146, "DRIVER ON FILE", driver_profile.name, meta, extra);
    snprintf(meta, sizeof(meta), "Seat  %s", passenger_profile.seat);
    snprintf(extra, sizeof(extra), "Contact  %s", passenger_profile.contact);
    identity_card(side, 338, 146, "PASSENGER ON FILE", passenger_profile.name, meta, extra);
}

static void build_banner(void)
{
    banner = make_card(screen_root, 16, 68, UI_W - 32, 116);
    banner_bar = make_dot(banner, 0, 0, SEV_FILL[SEV_INFO]);
    lv_obj_set_size(banner_bar, 6, 116);
    lv_obj_set_style_radius(banner_bar, 0, 0);
    banner_title = make_label(banner, "SYSTEM CHECK", &lv_font_montserrat_20, SEV_TEXT[SEV_INFO], 28, 18);
    lv_obj_set_width(banner_title, 980);
    banner_detail = make_label(banner, "Cabin safety is starting", &lv_font_montserrat_16, COL_TEXT, 28, 52);
    lv_obj_set_width(banner_detail, 980);
    banner_action = make_label(banner, "Please wait", &lv_font_montserrat_16, COL_MUTED, 28, 86);
    lv_obj_set_width(banner_action, 860);
    banner_time = make_label(banner, "0:00", &lv_font_montserrat_16, COL_MUTED, 1088, 18);
    ack_btn = make_button(banner, "Acknowledge", 1036, 54, 180, 48, COL_SURFACE_2,
                          &lv_font_montserrat_16, on_ack, NULL, NULL);
    lv_obj_add_flag(ack_btn, LV_OBJ_FLAG_HIDDEN);
    banner_on = 1;
    alert_since = lv_tick_get();
    snprintf(shown_title, sizeof(shown_title), "SYSTEM CHECK");
}

static void build_footer(void)
{
    footer = lv_obj_create(screen_root);
    lv_obj_set_pos(footer, 0, UI_H - FOOTER_H);
    lv_obj_set_size(footer, UI_W, FOOTER_H);
    flat(footer, COL_HEADER);
    lv_obj_set_style_border_side(footer, LV_BORDER_SIDE_TOP, 0);
    lv_obj_set_style_border_width(footer, 1, 0);
    lv_obj_set_style_border_color(footer, lv_color_hex(COL_LINE), 0);

    const char *names[3] = {"LIVE", "CABIN", "SYSTEM"};
    for(int i = 0; i < 3; i++) {
        nav_btn[i] = make_button(footer, names[i], 16 + i * 156, 8, 148, 48, COL_HEADER,
                                 &lv_font_montserrat_16, on_nav, (void *)(intptr_t)i, &nav_label[i]);
    }
    sos_btn = make_button(footer, "SOS", UI_W - 172, 8, 156, 48, COL_SOS, &lv_font_montserrat_20, on_sos, NULL, NULL);
    style_nav(VIEW_LIVE);
}

static void build_screen(void)
{
    load_roster();
    screen_root = lv_screen_active();
    lv_obj_set_size(screen_root, UI_W, UI_H);
    flat(screen_root, COL_BG);
    build_header();
    build_live();
    build_cabin();
    build_system();
    build_banner();
    build_footer();
    show_view(VIEW_LIVE);
    lv_obj_add_flag(view_cabin, LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_flag(view_system, LV_OBJ_FLAG_HIDDEN);
    layout_banner(SEV_INFO);
    lv_obj_move_foreground(banner);
    lv_obj_move_foreground(footer);
}

static void render_state(void)
{
    int level = severity_from(field_text[F_SAFETY]);
    paint_dot(accent, SEV_FILL[level]);
    paint_dot(safety_dot, SEV_FILL[level]);
    paint_text(safety_label, field_text[F_SAFETY], SEV_TEXT[level]);
    paint_text(hero, field_text[F_HEADLINE], hero_color(level, field_text[F_HEADLINE]));
    paint_text(presence, field_text[F_DRIVER], COL_MUTED);

    paint_reading(metric_value[0], field_text[F_EYES]);
    paint_reading(metric_value[1], field_text[F_GAZE]);
    paint_reading(metric_value[2], field_text[F_DROWSY]);
    paint_reading(metric_value[3], field_text[F_YAWN]);
    paint_reading(metric_value[4], field_text[F_ACTIVITY]);

    paint_text(live_occ, field_text[F_OCCUPANCY], COL_TEXT);
    paint_text(cabin_occ, field_text[F_OCCUPANCY], COL_TEXT);
    paint_reading(live_front, field_text[F_FRONT]);
    paint_reading(cabin_front, field_text[F_FRONT]);
    paint_reading(live_rear, field_text[F_REAR]);
    paint_reading(cabin_rear, field_text[F_REAR]);
    paint_reading(live_phone, field_text[F_PHONE]);
    paint_reading(cabin_phone, field_text[F_PHONE]);
    paint_reading(live_reach, field_text[F_REACH]);
    paint_reading(cabin_reach, field_text[F_REACH]);

    const char *cabin = field_text[F_CABIN];
    uint32_t cabin_color = status_color(cabin);
    paint_text(live_cabin, cabin_sentence_for(cabin), cabin_color);
    paint_text(cabin_sentence, cabin_sentence_for(cabin), cabin_color);
    paint_status(row_cabin, cabin);
    paint_dot(row_cabin_dot, cabin_color);

    paint_status(sys_word, field_text[F_SYSTEM]);
    paint_dot(sys_dot, status_color(field_text[F_SYSTEM]));
    paint_sensor(row_camera, field_text[F_CAMERA], 0);
    paint_dot(row_camera_dot, strcmp(field_text[F_CAMERA], "ok") == 0 ? SEV_FILL[SEV_SAFE] : SEV_FILL[SEV_FAULT]);
    paint_sensor(row_driver, field_text[F_DMS], 0);
    paint_dot(row_driver_dot, strcmp(field_text[F_DMS], "ok") == 0 ? SEV_FILL[SEV_SAFE] : SEV_FILL[SEV_FAULT]);

    paint_sensor(eng_camera, field_text[F_CAMERA], 0);
    paint_sensor(eng_mic, field_text[F_MIC], 1);
    paint_sensor(eng_gps, field_text[F_GPS], 1);
    paint_status(eng_model, field_text[F_MODEL]);
    paint_status(eng_record, field_text[F_RECORD]);
    paint_status(eng_link, field_text[F_LINK]);

    char buf[FIELD_CHARS + 8];
    snprintf(buf, sizeof(buf), "%s FPS", field_text[F_FPS]);
    paint_text(eng_fps, buf, COL_TEXT);
    if(strcmp(field_text[F_LATENCY], "--") == 0) snprintf(buf, sizeof(buf), "--");
    else snprintf(buf, sizeof(buf), "%s ms", field_text[F_LATENCY]);
    paint_text(eng_latency, buf, COL_TEXT);

    const char *note = field_text[F_VLM_TEXT][0] ? field_text[F_VLM_TEXT] : "No cabin note";
    paint_text(eng_note, note, field_text[F_VLM_TEXT][0] ? COL_TEXT : COL_MUTED);
    const char *decision = field_text[F_MESSAGE][0] ? field_text[F_MESSAGE] : "No decision yet";
    paint_text(eng_decision, decision, field_text[F_MESSAGE][0] ? COL_MUTED : COL_MUTED);
    paint_text(eng_vlm, field_text[F_VLM][0] ? field_text[F_VLM] : "idle", COL_TEXT);

    int show = level != SEV_SAFE && field_text[F_TITLE][0] != '\0';
    if(show && level != last_level) layout_banner(level);
    paint_text(banner_title, field_text[F_TITLE], SEV_TEXT[level]);
    paint_text(banner_detail, field_text[F_DETAIL], COL_TEXT);
    if(field_text[F_ACTION][0]) {
        lv_obj_remove_flag(banner_action, LV_OBJ_FLAG_HIDDEN);
        paint_text(banner_action, field_text[F_ACTION], COL_MUTED);
    } else {
        lv_obj_add_flag(banner_action, LV_OBJ_FLAG_HIDDEN);
    }
    if(strcmp(shown_title, field_text[F_TITLE]) != 0) {
        snprintf(shown_title, sizeof(shown_title), "%s", field_text[F_TITLE]);
        alert_since = lv_tick_get();
        if(alert_since == 0) alert_since = 1;
        set_label(banner_time, "0:00");
    }
    show_banner(show);

    if(level != last_level) {
        if(level >= SEV_ATTENTION) show_view(VIEW_LIVE);
        if(level == SEV_WARNING || level == SEV_CRITICAL) {
            incident_seen_at = lv_tick_get();
            if(incident_seen_at == 0) incident_seen_at = 1;
        }
        last_level = level;
    }
}

static int split_fields(char *line, char **out, int max)
{
    int count = 1;
    out[0] = line;
    for(char *cursor = line; *cursor; cursor++) {
        if(*cursor == '|') {
            *cursor = '\0';
            if(count < max) out[count] = cursor + 1;
            count++;
        }
    }
    return count;
}

static void apply_state(char *line)
{
    char *raw[F_COUNT];
    int count = split_fields(line, raw, F_COUNT);
    if(count < F_COUNT) return;
    for(int i = 0; i < F_COUNT; i++)
        snprintf(field_text[i], FIELD_CHARS, "%s", raw[i]);
    render_state();
}

static void drain_socket(void)
{
    if(sock_fd < 0) {
        sock_fd = socket(AF_INET, SOCK_STREAM, 0);
        if(sock_fd < 0) return;
        struct sockaddr_in addr;
        memset(&addr, 0, sizeof(addr));
        addr.sin_family = AF_INET;
        addr.sin_port = htons(PORT);
        inet_pton(AF_INET, "127.0.0.1", &addr.sin_addr);
        if(connect(sock_fd, (struct sockaddr *)&addr, sizeof(addr)) != 0) {
            close(sock_fd);
            sock_fd = -1;
            return;
        }
    }

    char temp[1024];
    for(;;) {
        ssize_t got = recv(sock_fd, temp, sizeof(temp), MSG_DONTWAIT);
        if(got == 0) {
            close(sock_fd);
            sock_fd = -1;
            return;
        }
        if(got < 0) {
            if(errno == EAGAIN || errno == EWOULDBLOCK) break;
            close(sock_fd);
            sock_fd = -1;
            return;
        }
        if(acc_len + (size_t)got >= sizeof(acc)) acc_len = 0;
        memcpy(acc + acc_len, temp, (size_t)got);
        acc_len += (size_t)got;
    }

    char *start = acc;
    char *end = memchr(start, '\n', acc_len);
    char *last = NULL;
    while(end) {
        *end = '\0';
        last = start;
        size_t used = (size_t)(end - acc) + 1;
        start = end + 1;
        end = memchr(start, '\n', acc_len - (size_t)(start - acc));
        if(!end) {
            apply_state(last);
            size_t keep = acc_len - used;
            memmove(acc, acc + used, keep);
            acc_len = keep;
            return;
        }
    }
}

static uint32_t poll_live(void)
{
    if(current_view != VIEW_CABIN) return 0;
    struct stat st;
    if(stat("results/live.bgr", &st) != 0) return 0;
    if(st.st_mtim.tv_sec == live_mtim.tv_sec && st.st_mtim.tv_nsec == live_mtim.tv_nsec) return 0;
    if(st.st_size != (off_t)(PREVIEW_W * PREVIEW_H * 3)) return 0;

    FILE *file = fopen("results/live.bgr", "rb");
    if(!file) return 0;
    uint8_t *bgr = malloc((size_t)st.st_size);
    if(!bgr) {
        fclose(file);
        return 0;
    }
    size_t got = fread(bgr, 1, (size_t)st.st_size, file);
    fclose(file);
    if(got != (size_t)st.st_size) {
        free(bgr);
        return 0;
    }

    struct timespec t0, t1;
    clock_gettime(CLOCK_MONOTONIC, &t0);
    uint8_t *dst = live_buf;
    const uint8_t *src = bgr;
    for(int i = 0; i < PREVIEW_W * PREVIEW_H; i++) {
        dst[0] = src[0];
        dst[1] = src[1];
        dst[2] = src[2];
        dst[3] = 0xFF;
        src += 3;
        dst += 4;
    }
    clock_gettime(CLOCK_MONOTONIC, &t1);
    free(bgr);
    live_mtim = st.st_mtim;
    lv_obj_invalidate(live_view);
    if(!preview_ready) {
        preview_ready = 1;
        paint_text(cabin_image_state, "Live image", SEV_TEXT[SEV_SAFE]);
    }
    return (uint32_t)((t1.tv_sec - t0.tv_sec) * 1000000L + (t1.tv_nsec - t0.tv_nsec) / 1000L);
}

static void refresh_clock(void)
{
    time_t now = time(NULL);
    struct tm parts;
    localtime_r(&now, &parts);
    char buf[8];
    snprintf(buf, sizeof(buf), "%02d:%02d", parts.tm_hour, parts.tm_min);
    if(strcmp(buf, clock_buf) == 0) return;
    snprintf(clock_buf, sizeof(clock_buf), "%s", buf);
    set_label(clock_label, clock_buf);
}

static void refresh_duration(void)
{
    if(!banner_on || alert_since == 0) return;
    uint32_t seconds = lv_tick_elaps(alert_since) / 1000U;
    char buf[16];
    snprintf(buf, sizeof(buf), "%u:%02u", seconds / 60U, seconds % 60U);
    set_label(banner_time, buf);
}

int main(void)
{
    mkdir_results();
    write_dms_view(0);
    lv_init();
    lv_display_t *disp = lv_x11_window_create("CMS", UI_W, UI_H);
    lv_x11_inputs_create(disp, NULL);
    {
        typedef struct {
            _x11_user_hdr_t hdr;
            Window window;
        } x11_window_hdr_t;
        x11_window_hdr_t *xd = lv_display_get_driver_data(disp);
        Cursor pointer = XCreateFontCursor(xd->hdr.display, XC_left_ptr);
        XDefineCursor(xd->hdr.display, xd->window, pointer);
        XFreeCursor(xd->hdr.display, pointer);
        XFlush(xd->hdr.display);
    }
    build_screen();
    printf("[UI] window open\n");
    fflush(stdout);

    uint32_t started = lv_tick_get();
    uint32_t perf_window = started;
    uint32_t worst_render = 0;
    uint32_t worst_convert = 0;
    uint32_t frames = 0;

    while(1) {
        struct timespec t0, t1;
        clock_gettime(CLOCK_MONOTONIC, &t0);
        lv_timer_handler();
        clock_gettime(CLOCK_MONOTONIC, &t1);
        uint32_t render_us = (uint32_t)((t1.tv_sec - t0.tv_sec) * 1000000L + (t1.tv_nsec - t0.tv_nsec) / 1000L);
        if(render_us > worst_render) worst_render = render_us;
        frames++;

        drain_socket();
        uint32_t convert_us = poll_live();
        if(convert_us > worst_convert) worst_convert = convert_us;
        refresh_clock();
        refresh_duration();

        if(!sos_pressed && incident_seen_at && getenv("CMS_UI_PRESS_SOS") &&
           lv_tick_elaps(incident_seen_at) > 1200) {
            sos_pressed = 1;
            lv_obj_send_event(sos_btn, LV_EVENT_CLICKED, NULL);
            printf("[UI] SOS button pressed\n");
            fflush(stdout);
        }
        if(lv_tick_elaps(perf_window) >= 5000) {
            printf("[UI] render worst %u us over %u frames, preview convert worst %u us\n",
                   worst_render, frames, worst_convert);
            fflush(stdout);
            worst_render = 0;
            worst_convert = 0;
            frames = 0;
            perf_window = lv_tick_get();
        }
        usleep(5000);
    }
    return 0;
}
