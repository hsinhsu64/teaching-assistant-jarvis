from .launcher import main

if __name__ == "__main__":  # Windows 開子行程時會以 __mp_main__ 重新載入，不能再啟動一次
    main()
