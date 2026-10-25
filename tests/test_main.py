from group_project_backend import main


def test_main_prints_greeting(capsys):
    main()

    captured = capsys.readouterr()
    assert captured.out == "Hello from group-project-backend!\n"
