#pragma once

class IApp {
public:
    virtual ~IApp() = default;
    virtual const char *Id() const = 0;
    virtual void Create() = 0;
    virtual void Destroy() = 0;
    virtual void Show() = 0;
    virtual void Hide() = 0;
    virtual void OnUserInteraction() {}
};
